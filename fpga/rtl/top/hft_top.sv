// ============================================================
// hft_top.sv — Top-level, Nexys Video (Artix-7)
//
// Pipeline (clock domains):
//
//   rgmii_rxc domain (MAC capture FFs only, BUFR, single region):
//     eth_mac_1g_rgmii internal RX/TX FIFOs (async)
//                    │
//   clk domain (MMCM 125 MHz, BUFG, global):
//     eth_stack_wrapper → market_data_parser → itch_msg_bridge
//     tcp_engine → soup_session                      │
//                                             symbol_router
//                                             /    |    \   \
//                                         ob[0] ob[1] ob[2] ob[3]
// ============================================================
`timescale 1ns/1ps
module hft_top
    import hft_pkg::*;
(
    input  logic        sys_clk,
    input  logic        sys_rst_n,

    // RGMII
    input  logic        rgmii_rxc,
    input  logic [3:0]  rgmii_rxd,
    input  logic        rgmii_rx_ctl,
    output logic        rgmii_txc,
    output logic [3:0]  rgmii_txd,
    output logic        rgmii_tx_ctl,

    // PHY management
    output logic        eth_mdc,
    inout  wire         eth_mdio,
    output logic        phy_rst_n,

    // Debug
    input  logic        uart_rx,
    output logic        uart_tx,
    output logic [3:0]  led
);

    // ── Clocks & reset ──────────────────────────────────────
    logic clk;        // 125 MHz, MMCM-derived (clk_unbuf domain)
    logic clk90;
    logic clk200;
    logic rst;        // synchronous, active-high, clk domain
    logic rst_n;      // active-low version for async-reset modules

    clk_rst clk_rst_inst (
        .sys_clk   (sys_clk),
        .sys_rst_n (sys_rst_n),
        .clk       (clk),
        .clk90     (clk90),
        .clk200    (clk200),
        .rst       (rst)
    );

    assign rst_n = ~rst;

    // rgmii_rxc domain reset: synchronize rst into rgmii_rxc
    logic rxc_rst_n;
    logic rxc_rst_sync1, rxc_rst_sync2;
    always_ff @(posedge rgmii_rxc or posedge rst) begin
        if (rst) begin
            rxc_rst_sync1 <= 1'b0;
            rxc_rst_sync2 <= 1'b0;
        end else begin
            rxc_rst_sync1 <= 1'b1;
            rxc_rst_sync2 <= rxc_rst_sync1;
        end
    end
    assign rxc_rst_n = rxc_rst_sync2;

    // ── IDELAYCTRL ──────────────────────────────────────────
    (* IODELAY_GROUP = "rgmii_idelay" *)
    IDELAYCTRL idelayctrl_inst (
        .REFCLK (clk200),
        .RST    (rst),
        .RDY    ()
    );

    // ── AXI-Stream interfaces (clk domain) ──────────────────
    axis_if #(.DATA_W(8)) mac_rx (.clk(clk));
    // mac_tx is driven by mac_tx_arbiter (tcp_engine p0, udp_stack p1)
    axis_if #(.DATA_W(8)) mac_tx_tcp  (.clk(clk));  // tcp_engine TX
    axis_if #(.DATA_W(8)) mac_tx_udp  (.clk(clk));  // eth_stack TX
    axis_if #(.DATA_W(8)) mac_tx      (.clk(clk));  // → RGMII MAC
    axis_if #(.DATA_W(8)) udp_rx (.clk(clk));
    axis_if #(.DATA_W(8)) udp_tx (.clk(clk));

    // ── UDP sideband ─────────────────────────────────────────
    logic [15:0] udp_rx_src_port, udp_rx_dst_port;
    logic [47:0] udp_tx_dst_mac;
    logic [31:0] udp_tx_dst_ip;
    logic [15:0] udp_tx_src_port, udp_tx_dst_port, udp_tx_length;

    // ── Ethernet MAC ─────────────────────────────────────────
    // logic_clk=clk: user-facing FIFOs run in clk domain.
    // BUFR drives only the RGMII capture FFs (one clock region, correct for 7-series).
    // eth_mac_1g_rgmii_fifo's internal async FIFOs bridge rgmii_rxc ↔ clk.
    eth_mac_1g_rgmii_fifo #(
        .TARGET             ("XILINX"),
        .IODDR_STYLE        ("IODDR"),
        .CLOCK_INPUT_STYLE  ("BUFR"),
        .USE_CLK90          ("TRUE"),
        .ENABLE_PADDING     (1),
        .MIN_FRAME_LENGTH   (64),
        .TX_FIFO_DEPTH      (4096),
        .RX_FIFO_DEPTH      (4096)
    ) eth_mac_inst (
        .gtx_clk            (clk),
        .gtx_clk90          (clk90),
        .gtx_rst            (rst),
        .logic_clk          (clk),
        .logic_rst          (rst),
        .rgmii_rx_clk       (rgmii_rxc),
        .rgmii_rxd          (rgmii_rxd),
        .rgmii_rx_ctl       (rgmii_rx_ctl),
        .rgmii_tx_clk       (rgmii_txc),
        .rgmii_txd          (rgmii_txd),
        .rgmii_tx_ctl       (rgmii_tx_ctl),
        .tx_axis_tdata      (mac_tx.tdata),   // driven by mac_tx_arbiter
        .tx_axis_tkeep      (1'b1),
        .tx_axis_tvalid     (mac_tx.tvalid),
        .tx_axis_tready     (mac_tx.tready),
        .tx_axis_tlast      (mac_tx.tlast),
        .tx_axis_tuser      (mac_tx.tuser),
        .rx_axis_tdata      (mac_rx.tdata),
        .rx_axis_tvalid     (mac_rx.tvalid),
        .rx_axis_tready     (mac_rx.tready),
        .rx_axis_tlast      (mac_rx.tlast),
        .rx_axis_tuser      (mac_rx.tuser),
        .cfg_ifg            (8'd12),
        .cfg_tx_enable      (1'b1),
        .cfg_rx_enable      (1'b1),
        .tx_error_underflow (),
        .tx_fifo_overflow   (),
        .tx_fifo_bad_frame  (),
        .tx_fifo_good_frame (),
        .rx_error_bad_frame (),
        .rx_error_bad_fcs   (),
        .rx_fifo_overflow   (),
        .rx_fifo_bad_frame  (),
        .rx_fifo_good_frame (),
        .speed              ()
    );

    // ── MAC TX: tcp_engine direct (UDP bypassed for Phase 27 debug) ──
    assign mac_tx.tdata  = mac_tx_tcp.tdata;
    assign mac_tx.tvalid = mac_tx_tcp.tvalid;
    assign mac_tx.tlast  = mac_tx_tcp.tlast;
    assign mac_tx.tuser  = 1'b0;
    assign mac_tx_tcp.tready = mac_tx.tready;
    assign mac_tx_udp.tready = 1'b1;  // drain UDP silently

    // ── Ethernet stack ARP/IP/UDP (clk domain) ──────────────
    // mac_rx is broadcast to both eth_stack_wrapper and tcp_engine.
    // eth_stack_wrapper drives mac_rx.tready (flow control).
    // tcp_engine is always-ready on RX so it never needs to backpressure.
    eth_stack_wrapper eth_stack_inst (
        .clk             (clk),
        .rst             (rst),
        .mac_rx_tdata    (mac_rx.tdata),
        .mac_rx_tvalid   (mac_rx.tvalid),
        .mac_rx_tready   (),              // driven by tcp_engine (= 1'b1)
        .mac_rx_tlast    (mac_rx.tlast),
        .mac_rx_tuser    (mac_rx.tuser),
        .mac_tx_tdata    (mac_tx_udp.tdata),
        .mac_tx_tvalid   (mac_tx_udp.tvalid),
        .mac_tx_tready   (mac_tx_udp.tready),
        .mac_tx_tlast    (mac_tx_udp.tlast),
        .mac_tx_tuser    (mac_tx_udp.tuser),
        .udp_rx_tdata    (udp_rx.tdata),
        .udp_rx_tvalid   (udp_rx.tvalid),
        .udp_rx_tready   (udp_rx.tready),
        .udp_rx_tlast    (udp_rx.tlast),
        .udp_rx_tuser    (udp_rx.tuser),
        .udp_rx_src_port (udp_rx_src_port),
        .udp_rx_dst_port (udp_rx_dst_port),
        .udp_tx_tdata    (udp_tx.tdata),
        .udp_tx_tvalid   (udp_tx.tvalid),
        .udp_tx_tready   (udp_tx.tready),
        .udp_tx_tlast    (udp_tx.tlast),
        .udp_tx_tuser    (udp_tx.tuser),
        .udp_tx_dst_mac  (udp_tx_dst_mac),
        .udp_tx_dst_ip   (udp_tx_dst_ip),
        .udp_tx_src_port (udp_tx_src_port),
        .udp_tx_dst_port (udp_tx_dst_port),
        .udp_tx_length   (udp_tx_length)
    );

    // ── Market data parser (clk domain) ─────────────────────
    // Decodes UDP byte stream → quote_t with op=OP_ADD
    quote_t w_quote;
    logic   w_quote_valid;

    market_data_parser mkt_parser_inst (
        .clk         (clk),
        .rst         (rst),
        .rx_tdata    (mac_rx.tdata),   // parse raw MAC frames directly
        .rx_tvalid   (mac_rx.tvalid),
        .rx_tready   (),               // always 1 internally; mac_rx.tready driven by tcp_engine
        .rx_tlast    (mac_rx.tlast),
        .rx_tuser    (mac_rx.tuser),
        .rx_dst_port (),               // unused — port parsed from frame
        .quote_out   (w_quote),
        .quote_valid (w_quote_valid)
    );

    // ── Quote bridge (clk domain, same-clock FIFO) ──────────
    quote_t r_quote;
    logic   r_quote_valid;
    logic   w_drop;
    logic [15:0] w_drop_count;

    itch_msg_bridge cdc_bridge_inst (
        .wclk          (clk),
        .wrst_n        (rst_n),
        .w_quote       (w_quote),
        .w_quote_valid (w_quote_valid),
        .w_drop        (w_drop),
        .w_drop_count  (w_drop_count),
        .rclk          (clk),
        .rrst_n        (rst_n),
        .r_quote       (r_quote),
        .r_quote_valid (r_quote_valid),
        .fifo_empty    (),
        .fifo_full_rclk()
    );

    // ── Symbol router (clk domain) ──────────────────────────
    // Routes r_quote to one of 4 order book slots by symbol_id
    quote_t book_in [0:3];

    symbol_router #(.N_BOOKS(4)) sym_router_inst (
        .quote_in (r_quote),
        .book     (book_in)
    );

    // ── UART RX config (clk domain) ─────────────────────────
    logic        uart_wr_en;
    logic [1:0]  uart_wr_slot;
    logic [31:0] uart_wr_base;
    logic        uart_wr_offset_en;
    logic [1:0]  uart_wr_offset_slot;
    logic [31:0] uart_wr_offset_val;

    logic kill_switch;

    uart_rx_config #(.N_BOOKS(4)) uart_cfg_inst (
        .clk            (clk),
        .rst            (rst),
        .uart_rx        (uart_rx),
        .wr_en          (uart_wr_en),
        .wr_slot        (uart_wr_slot),
        .wr_base        (uart_wr_base),
        .wr_offset_en   (uart_wr_offset_en),
        .wr_offset_slot (uart_wr_offset_slot),
        .wr_offset_val  (uart_wr_offset_val),
        .kill_switch    (kill_switch)
    );

    // ── Quote offset register file (clk domain) ──────────────
    // Per-slot quote offset in price ticks. Defaults match backtest-optimal
    // values: AVAX=3, LINK=10, AAVE=5, INJ=3. Updated live via 0xC0 UART frame.
    logic [31:0] quote_offset [0:3];

    always_ff @(posedge clk) begin
        if (rst) begin
            quote_offset[0] <= 32'd3;   // AVAX
            quote_offset[1] <= 32'd10;  // LINK
            quote_offset[2] <= 32'd5;   // AAVE
            quote_offset[3] <= 32'd3;   // INJ
        end else if (uart_wr_offset_en) begin
            quote_offset[uart_wr_offset_slot] <= uart_wr_offset_val;
        end
    end

    // ── Price base table (clk domain) ───────────────────────
    logic [31:0] price_base [0:3];

    price_base_table #(
        .N_BOOKS(4),
        .BASE0  (1_000_000),   // slot 0: $100.00
        .BASE1  (2_000_000),   // slot 1: $200.00
        .BASE2  (  500_000),   // slot 2:  $50.00
        .BASE3  (  150_000)    // slot 3:  $15.00
    ) price_base_inst (
        .clk       (clk),
        .rst       (rst),
        .wr_en     (uart_wr_en),
        .wr_slot   (uart_wr_slot),
        .wr_base   (uart_wr_base),
        .price_base(price_base)
    );

    // ── Order books × 4 (clk domain) ────────────────────────
    logic [31:0] best_bid_price [0:3];
    logic [31:0] best_ask_price [0:3];
    logic [31:0] best_bid_qty   [0:3];
    logic [31:0] best_ask_qty   [0:3];
    logic [31:0] spread         [0:3];
    logic [31:0] mid_price      [0:3];
    logic        bid_valid      [0:3];
    logic        ask_valid      [0:3];

    generate
        for (genvar i = 0; i < 4; i++) begin : gen_order_books
            order_book #(.MAX_LEVELS(256)) ob_inst (
                .clk            (clk),
                .rst            (rst),
                .quote_in       (book_in[i]),
                .quote_valid    (r_quote_valid && book_in[i].valid),
                .price_base     (price_base[i]),
                .best_bid_price (best_bid_price[i]),
                .best_ask_price (best_ask_price[i]),
                .best_bid_qty   (best_bid_qty[i]),
                .best_ask_qty   (best_ask_qty[i]),
                .spread         (spread[i]),
                .mid_price      (mid_price[i]),
                .bid_valid      (bid_valid[i]),
                .ask_valid      (ask_valid[i])
            );
        end
    endgenerate

    // ── OUCH ACK receiver — UDP path (paper mode) ───────────
    logic        udp_ack_valid;
    logic [63:0] udp_ack_order_id;
    logic [7:0]  udp_ack_status;
    logic [31:0] udp_ack_fill_qty;

    ouch_ack_receiver ack_rx_inst (
        .clk          (clk),
        .rst          (rst),
        .rx_tdata     (udp_rx.tdata),
        .rx_tvalid    (udp_rx.tvalid),
        .rx_tlast     (udp_rx.tlast),
        .rx_dst_port  (udp_rx_dst_port),
        .ack_valid    (udp_ack_valid),
        .ack_order_id (udp_ack_order_id),
        .ack_status   (udp_ack_status),
        .ack_fill_qty (udp_ack_fill_qty)
    );

    // ── TCP engine (clk domain) ──────────────────────────────
    // mac_rx is broadcast: both eth_stack_wrapper and tcp_engine see all frames.
    // tcp_engine ignores non-TCP; eth_stack_wrapper ignores TCP.
    logic [7:0]  tcp_app_tx_tdata;
    logic        tcp_app_tx_tvalid;
    logic        tcp_app_tx_tready;
    logic        tcp_app_tx_tlast;
    logic [7:0]  tcp_app_rx_tdata;
    logic        tcp_app_rx_tvalid;
    logic        tcp_app_rx_tready;
    logic        tcp_app_rx_tlast;
    logic        tcp_connected;

    tcp_engine #(
        .LOCAL_MAC   (LOCAL_MAC),
        .LOCAL_IP    (LOCAL_IP),
        .GATEWAY_IP  (GATEWAY_IP),
        .SERVER_IP   (OUCH_DST_IP),
        .LOCAL_PORT  (16'd4201),
        .SERVER_PORT (PORT_OUCH_TCP)
    ) u_tcp (
        .clk            (clk),
        .rst_n          (rst_n),
        .mac_rx_tdata   (mac_rx.tdata),
        .mac_rx_tvalid  (mac_rx.tvalid),
        .mac_rx_tready  (mac_rx.tready),  // tcp_engine drives tready=1'b1 (always ready)
        .mac_rx_tlast   (mac_rx.tlast),
        .mac_rx_tuser   (mac_rx.tuser),
        .mac_tx_tdata   (mac_tx_tcp.tdata),
        .mac_tx_tvalid  (mac_tx_tcp.tvalid),
        .mac_tx_tready  (mac_tx_tcp.tready),
        .mac_tx_tlast   (mac_tx_tcp.tlast),
        .mac_tx_tuser   (),
        .app_tx_tdata   (tcp_app_tx_tdata),
        .app_tx_tvalid  (tcp_app_tx_tvalid),
        .app_tx_tready  (tcp_app_tx_tready),
        .app_tx_tlast   (tcp_app_tx_tlast),
        .app_rx_tdata   (tcp_app_rx_tdata),
        .app_rx_tvalid  (tcp_app_rx_tvalid),
        .app_rx_tready  (tcp_app_rx_tready),
        .app_rx_tlast   (tcp_app_rx_tlast),
        .connected      (tcp_connected),
        .arp_done       (tcp_arp_done)
    );
    logic tcp_arp_done;

    // ── SoupBinTCP session (clk domain) ─────────────────────
    logic        soup_session_active;
    logic        tcp_ack_valid;
    logic [63:0] tcp_ack_order_id;
    logic [7:0]  tcp_ack_status;
    logic [31:0] tcp_ack_fill_qty;
    logic [7:0]  ouch_tx_tdata;
    logic        ouch_tx_tvalid;
    logic        ouch_tx_tready;
    logic        ouch_tx_tlast;

    soup_session u_soup (
        .clk            (clk),
        .rst_n          (rst_n),
        .tcp_connected  (tcp_connected),
        .app_tx_tdata   (tcp_app_tx_tdata),
        .app_tx_tvalid  (tcp_app_tx_tvalid),
        .app_tx_tready  (tcp_app_tx_tready),
        .app_tx_tlast   (tcp_app_tx_tlast),
        .app_rx_tdata   (tcp_app_rx_tdata),
        .app_rx_tvalid  (tcp_app_rx_tvalid),
        .app_rx_tready  (tcp_app_rx_tready),
        .app_rx_tlast   (tcp_app_rx_tlast),
        .ouch_tdata     (ouch_tx_tdata),
        .ouch_tvalid    (ouch_tx_tvalid),
        .ouch_tready    (ouch_tx_tready),
        .ouch_tlast     (ouch_tx_tlast),
        .ack_valid      (tcp_ack_valid),
        .ack_order_id   (tcp_ack_order_id),
        .ack_status     (tcp_ack_status),
        .ack_fill_qty   (tcp_ack_fill_qty),
        .session_active (soup_session_active),
        .drop_count     ()
    );

    // ── ACK mux: TCP (live) takes priority over UDP (paper) ──
    // When TCP session is active and sends ACK, use it.
    // Fallback to UDP ACK (ack_simulator.py) when TCP not active.
    logic        ack_raw_valid;
    logic [63:0] ack_raw_order_id;
    logic [7:0]  ack_raw_status;
    logic [31:0] ack_raw_fill_qty;

    assign ack_raw_valid    = tcp_ack_valid    | udp_ack_valid;
    assign ack_raw_order_id = tcp_ack_valid ? tcp_ack_order_id : udp_ack_order_id;
    assign ack_raw_status   = tcp_ack_valid ? tcp_ack_status   : udp_ack_status;
    assign ack_raw_fill_qty = tcp_ack_valid ? tcp_ack_fill_qty : udp_ack_fill_qty;

    // ── Phase 25: Latency monitor (clk domain) ──────────────
    logic        order_sent_pulse;
    logic [31:0] lat_min, lat_max, lat_last, lat_count;

    latency_monitor u_lat_mon (
        .clk       (clk),
        .rst       (rst),
        .quote_in  (r_quote_valid),
        .order_out (order_sent_pulse),
        .lat_min   (lat_min),
        .lat_max   (lat_max),
        .lat_last  (lat_last),
        .lat_count (lat_count)
    );

    // ── Order engine: strategy + CDC + OUCH encoder ─────────
    order_engine #(
        .N_BOOKS       (4),
        .SPREAD_MAX    (20000),
        .ORDER_QTY     (100),
        .MAX_POSITION  (1000),
        .COOLDOWN_CYC  (12_500_000),
        .SKEW_SHIFT    (3),            // inv_skew = position>>3 (~12 ticks at pos=100)
        .FAT_FINGER_BPS(500),
        .MAX_BURST     (5),
        .REFILL_PERIOD (12_500_000),
        .STALE_MULT    (3)             // stale_thresh = quote_offset × 3
    ) order_engine_inst (
        .clk              (clk),
        .rst              (rst),
        .rst_n            (rst_n),
        .best_bid_price   (best_bid_price),
        .best_ask_price   (best_ask_price),
        .mid_price        (mid_price),
        .spread           (spread),
        .bid_valid        (bid_valid),
        .ask_valid        (ask_valid),
        .quote_offset     (quote_offset),
        .kill_switch      (kill_switch),
        .enc_clk          (clk),
        .enc_rst_n        (rst_n),
        // OUCH 4.2 → soup_session → tcp_engine
        .ouch_tx_tdata    (ouch_tx_tdata),
        .ouch_tx_tvalid   (ouch_tx_tvalid),
        .ouch_tx_tready   (ouch_tx_tready),
        .ouch_tx_tlast    (ouch_tx_tlast),
        // UDP TX: telemetry only
        .udp_tx_tdata     (udp_tx.tdata),
        .udp_tx_tvalid    (udp_tx.tvalid),
        .udp_tx_tready    (udp_tx.tready),
        .udp_tx_tlast     (udp_tx.tlast),
        .udp_tx_tuser     (udp_tx.tuser),
        .udp_tx_dst_mac   (udp_tx_dst_mac),
        .udp_tx_dst_ip    (udp_tx_dst_ip),
        .udp_tx_src_port  (udp_tx_src_port),
        .udp_tx_dst_port  (udp_tx_dst_port),
        .udp_tx_length    (udp_tx_length),
        .ack_raw_valid    (ack_raw_valid),
        .ack_raw_order_id (ack_raw_order_id),
        .ack_raw_status   (ack_raw_status),
        .ack_raw_fill_qty (ack_raw_fill_qty),
        .lat_min          (lat_min),
        .lat_max          (lat_max),
        .lat_last         (lat_last),
        .lat_count        (lat_count),
        .drop_count       (),
        .order_sent       (order_sent_pulse)
    );

    // ── PHY reset ───────────────────────────────────────────
    phy_reset_ctrl #(.HOLD_CYCLES(PHY_RESET_CYCLES)) phy_rst_inst (
        .clk       (clk),
        .rst       (rst),
        .phy_rst_n (phy_rst_n)
    );

    // ── MDIO stub ───────────────────────────────────────────
    assign eth_mdc  = 1'b0;
    assign eth_mdio = 1'bz;

    // ── UART TX (loopback echo for host feedback) ───────────
    assign uart_tx = uart_rx;

    // ── Debug LEDs (clk domain) ─────────────────────────────
    // led[0] = FPGA running (rst deasserted)
    // led[1] = TCP connected to exchange
    // led[0] = FPGA running (rst deasserted)
    // led[1] = TCP connected to exchange
    // led[2] = soup_session active (Login Accepted processed)
    // led[3] = order book active (bid_valid on any slot)
    always_ff @(posedge clk) begin
        led[0] <= ~rst;
        led[1] <= tcp_connected;
        led[2] <= soup_session_active;
        led[3] <= bid_valid[0] | bid_valid[1] | bid_valid[2] | bid_valid[3];
    end

endmodule