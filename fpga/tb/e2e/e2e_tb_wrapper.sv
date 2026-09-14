// ============================================================
// e2e_tb_wrapper.sv — End-to-end pipeline wrapper for Phase 26
//
// Instantiates the full HFT pipeline without the Ethernet MAC
// or eth_stack_wrapper (3rd-party IP).  Drives and captures
// at the UDP payload level so cocotb only deals with byte streams.
//
// Clock domains:
//   rxc_clk — market_data_parser, itch_msg_bridge write side,
//              order_cdc_bridge read side, ouch_encoder
//   clk     — itch_msg_bridge read side, symbol_router,
//              order_book × 4, strategy, latency_monitor
//
// Pipeline flow:
//   UDP RX bytes (rxc_clk)
//     → market_data_parser (rxc_clk)
//       → itch_msg_bridge (CDC: rxc_clk → clk)
//         → symbol_router (clk, combinational)
//           → order_book × 4 (clk)
//             → order_engine: strategy + CDC + ouch_encoder
//   UDP TX bytes (rxc_clk) = OUCH new-order / cancel frames
// ============================================================
`timescale 1ns/1ps
module e2e_tb_wrapper
    import hft_pkg::*;
#(
    parameter int SPREAD_MAX    = 200,
    parameter int COOLDOWN_CYC  = 10,
    parameter int TIMEOUT_CYC   = 10_000,
    parameter int SKEW_SHIFT    = 31,        // 31 = inventory skew disabled
    parameter int QUOTE_OFFSET  = 2,
    parameter int FAT_FINGER_BPS = 500,
    parameter int MAX_BURST     = 5,
    parameter int REFILL_PERIOD = 200,
    parameter int STALE_MULT    = 3,
    parameter int PRICE_BASE    = 0          // same for all 4 slots
)(
    // ---- Clocks & resets -----------------------------------
    input  logic        rxc_clk,
    input  logic        rxc_rst_n,   // active-low, rxc domain
    input  logic        clk,
    input  logic        rst,         // active-high, clk domain

    // ---- UDP RX AXI-Stream (rxc_clk domain) ----------------
    input  logic [7:0]  rx_tdata,
    input  logic        rx_tvalid,
    output logic        rx_tready,
    input  logic        rx_tlast,
    input  logic        rx_tuser,
    input  logic [15:0] rx_dst_port,

    // ---- UDP TX AXI-Stream (rxc_clk domain) = OUCH out ----
    output logic [7:0]  tx_tdata,
    output logic        tx_tvalid,
    input  logic        tx_tready,
    output logic        tx_tlast,

    // ---- ACK inputs (rxc_clk domain) -----------------------
    input  logic        ack_raw_valid,
    input  logic [63:0] ack_raw_order_id,
    input  logic [7:0]  ack_raw_status,
    input  logic [31:0] ack_raw_fill_qty,

    // ---- Control (clk domain) ------------------------------
    input  logic        kill_switch,

    // ---- Phase 25 order pulse (clk domain) -----------------
    output logic        order_sent    // 1-cycle pulse when strategy fires
);

    logic rst_n;
    assign rst_n = ~rst;

    // ---- Market data parser (rxc_clk domain) ---------------
    quote_t w_quote;
    logic   w_quote_valid;
    logic   w_rx_tready;
    assign rx_tready = w_rx_tready;

    market_data_parser u_parser (
        .clk         (rxc_clk),
        .rst         (~rxc_rst_n),
        .rx_tdata    (rx_tdata),
        .rx_tvalid   (rx_tvalid),
        .rx_tready   (w_rx_tready),
        .rx_tlast    (rx_tlast),
        .rx_tuser    (rx_tuser),
        .rx_dst_port (rx_dst_port),
        .quote_out   (w_quote),
        .quote_valid (w_quote_valid)
    );

    // ---- CDC bridge (rxc_clk → clk) ------------------------
    quote_t r_quote;
    logic   r_quote_valid;

    itch_msg_bridge u_cdc (
        .wclk          (rxc_clk),
        .wrst_n        (rxc_rst_n),
        .w_quote       (w_quote),
        .w_quote_valid (w_quote_valid),
        .w_drop        (),
        .w_drop_count  (),
        .rclk          (clk),
        .rrst_n        (rst_n),
        .r_quote       (r_quote),
        .r_quote_valid (r_quote_valid),
        .fifo_empty    (),
        .fifo_full_rclk()
    );

    // ---- Symbol router: per-slot assign (avoids icarus always_comb loop issue) ---
    quote_t book_in [0:3];
    // Each slot independently checks symbol_id; avoids unpacked-array loop in always_comb
    assign book_in[0] = (r_quote_valid && r_quote.valid && r_quote.symbol_id == 16'd0) ? r_quote : '0;
    assign book_in[1] = (r_quote_valid && r_quote.valid && r_quote.symbol_id == 16'd1) ? r_quote : '0;
    assign book_in[2] = (r_quote_valid && r_quote.valid && r_quote.symbol_id == 16'd2) ? r_quote : '0;
    assign book_in[3] = (r_quote_valid && r_quote.valid && r_quote.symbol_id == 16'd3) ? r_quote : '0;

    // ---- Price base and quote offset: flat assigns (avoids icarus always_comb loop) ---
    logic [31:0] price_base  [0:3];
    logic [31:0] quote_offset [0:3];
    assign price_base[0]   = 32'(PRICE_BASE);   assign price_base[1]   = 32'(PRICE_BASE);
    assign price_base[2]   = 32'(PRICE_BASE);   assign price_base[3]   = 32'(PRICE_BASE);
    assign quote_offset[0] = 32'(QUOTE_OFFSET); assign quote_offset[1] = 32'(QUOTE_OFFSET);
    assign quote_offset[2] = 32'(QUOTE_OFFSET); assign quote_offset[3] = 32'(QUOTE_OFFSET);

    // ---- Order books × 4 (clk domain) ---------------------
    logic [31:0] best_bid_price [0:3];
    logic [31:0] best_ask_price [0:3];
    logic [31:0] best_bid_qty   [0:3];
    logic [31:0] best_ask_qty   [0:3];
    logic [31:0] spread         [0:3];
    logic [31:0] mid_price      [0:3];
    logic        bid_valid      [0:3];
    logic        ask_valid      [0:3];

    // order_book checks quote_in.valid internally; pass r_quote_valid to all slots
    generate
        for (genvar i = 0; i < 4; i++) begin : gen_books
            order_book #(.MAX_LEVELS(256)) u_ob (
                .clk            (clk),
                .rst            (rst),
                .quote_in       (book_in[i]),
                .quote_valid    (r_quote_valid),
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

    // ---- Latency monitor (clk domain) ----------------------
    logic [31:0] lat_min, lat_max, lat_last, lat_count;
    logic        order_sent_pulse;

    latency_monitor u_lat (
        .clk       (clk),
        .rst       (rst),
        .quote_in  (r_quote_valid),
        .order_out (order_sent_pulse),
        .lat_min   (lat_min),
        .lat_max   (lat_max),
        .lat_last  (lat_last),
        .lat_count (lat_count)
    );

    assign order_sent = order_sent_pulse;

    // ---- Order engine (strategy + CDC + OUCH encoder) ------
    // Phase 27 split the order_engine outputs: OUCH order frames leave on
    // ouch_tx_*, while udp_tx_* carries telemetry only. This testbench wants
    // the order stream, so it exposes ouch_tx_* as tx_*.
    logic [7:0]  ouch_tx_tdata;
    logic        ouch_tx_tvalid;
    logic        ouch_tx_tready;
    logic        ouch_tx_tlast;

    logic [7:0]  udp_tx_tdata;
    logic        udp_tx_tvalid;
    logic        udp_tx_tready;
    logic        udp_tx_tlast;
    logic        udp_tx_tuser;
    logic [47:0] udp_tx_dst_mac;
    logic [31:0] udp_tx_dst_ip;
    logic [15:0] udp_tx_src_port;
    logic [15:0] udp_tx_dst_port;
    logic [15:0] udp_tx_length;

    order_engine #(
        .N_BOOKS        (4),
        .SPREAD_MAX     (SPREAD_MAX),
        .ORDER_QTY      (100),
        .MAX_POSITION   (1000),
        .COOLDOWN_CYC   (COOLDOWN_CYC),
        .SKEW_SHIFT     (SKEW_SHIFT),
        .FAT_FINGER_BPS (FAT_FINGER_BPS),
        .MAX_BURST      (MAX_BURST),
        .REFILL_PERIOD  (REFILL_PERIOD),
        .STALE_MULT     (STALE_MULT)
    ) u_order_engine (
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
        .enc_clk          (rxc_clk),
        .enc_rst_n        (rxc_rst_n),
        .ouch_tx_tdata    (ouch_tx_tdata),
        .ouch_tx_tvalid   (ouch_tx_tvalid),
        .ouch_tx_tready   (ouch_tx_tready),
        .ouch_tx_tlast    (ouch_tx_tlast),
        .udp_tx_tdata     (udp_tx_tdata),
        .udp_tx_tvalid    (udp_tx_tvalid),
        .udp_tx_tready    (udp_tx_tready),
        .udp_tx_tlast     (udp_tx_tlast),
        .udp_tx_tuser     (udp_tx_tuser),
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

    // ---- Expose the OUCH order stream as tx_* ---------------
    assign tx_tdata        = ouch_tx_tdata;
    assign tx_tvalid       = ouch_tx_tvalid;
    assign ouch_tx_tready  = tx_tready;
    assign tx_tlast        = ouch_tx_tlast;

    // Telemetry path is unused here, but must not backpressure the encoder.
    assign udp_tx_tready   = 1'b1;

endmodule
