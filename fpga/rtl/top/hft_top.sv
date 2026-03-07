// ============================================================
// hft_top.sv — Top-level, Nexys Video (Artix-7)
// ============================================================
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

    // ---- Clocks & reset ------------------------------------
    logic clk;
    logic clk90;
    logic clk200;
    logic rst;

    clk_rst clk_rst_inst (
        .sys_clk    (sys_clk),
        .sys_rst_n  (sys_rst_n),
        .clk        (clk),
        .clk90      (clk90),
        .clk200     (clk200),
        .rst        (rst)
    );

    // ---- IDELAYCTRL ----------------------------------------
    (* IODELAY_GROUP = "rgmii_idelay" *)
    IDELAYCTRL idelayctrl_inst (
        .REFCLK (clk200),
        .RST    (rst),
        .RDY    ()
    );

    // ---- AXI-Stream interfaces -----------------------------
    axis_if #(.DATA_W(8)) mac_rx (.clk(clk));
    axis_if #(.DATA_W(8)) mac_tx (.clk(clk));
    axis_if #(.DATA_W(8)) udp_rx (.clk(clk));
    axis_if #(.DATA_W(8)) udp_tx (.clk(clk));

    // ---- UDP sideband signals ------------------------------
    logic [15:0] udp_rx_src_port, udp_rx_dst_port;
    logic [47:0] udp_tx_dst_mac;
    logic [31:0] udp_tx_dst_ip;
    logic [15:0] udp_tx_src_port, udp_tx_dst_port, udp_tx_length;

    // ---- Market data pipeline signals ----------------------
    quote_t      quote_out;
    logic        quote_valid;

    // ---- 4 order book inputs (one per symbol slot) ---------
    quote_t      book_in [0:3];

    // ---- Order book outputs --------------------------------
    logic [31:0] best_bid_price [0:3];
    logic [31:0] best_ask_price [0:3];
    logic [31:0] best_bid_size  [0:3];
    logic [31:0] best_ask_size  [0:3];

    // ---- Ethernet MAC --------------------------------------
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
        // RGMII — note renamed ports vs older verilog-ethernet
        .rgmii_rx_clk       (rgmii_rxc),
        .rgmii_rxd          (rgmii_rxd),
        .rgmii_rx_ctl       (rgmii_rx_ctl),
        .rgmii_tx_clk       (rgmii_txc),
        .rgmii_txd          (rgmii_txd),
        .rgmii_tx_ctl       (rgmii_tx_ctl),
        // AXI-Stream TX
        .tx_axis_tdata      (mac_tx.tdata),
        .tx_axis_tkeep      (1'b1),
        .tx_axis_tvalid     (mac_tx.tvalid),
        .tx_axis_tready     (mac_tx.tready),
        .tx_axis_tlast      (mac_tx.tlast),
        .tx_axis_tuser      (mac_tx.tuser),
        // AXI-Stream RX
        .rx_axis_tdata      (mac_rx.tdata),
        .rx_axis_tvalid     (mac_rx.tvalid),
        .rx_axis_tready     (mac_rx.tready),
        .rx_axis_tlast      (mac_rx.tlast),
        .rx_axis_tuser      (mac_rx.tuser),
        // Config — replaces ifg_delay
        .cfg_ifg            (8'd12),
        .cfg_tx_enable      (1'b1),
        .cfg_rx_enable      (1'b1),
        // Status — unused
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

    // ---- Ethernet stack (ARP/IP/UDP) -----------------------
    eth_stack_wrapper eth_stack_inst (
        .clk                (clk),
        .rst                (rst),
        .mac_rx_tdata       (mac_rx.tdata),
        .mac_rx_tvalid      (mac_rx.tvalid),
        .mac_rx_tready      (mac_rx.tready),
        .mac_rx_tlast       (mac_rx.tlast),
        .mac_rx_tuser       (mac_rx.tuser),
        .mac_tx_tdata       (mac_tx.tdata),
        .mac_tx_tvalid      (mac_tx.tvalid),
        .mac_tx_tready      (mac_tx.tready),
        .mac_tx_tlast       (mac_tx.tlast),
        .mac_tx_tuser       (mac_tx.tuser),
        .udp_rx_tdata       (udp_rx.tdata),
        .udp_rx_tvalid      (udp_rx.tvalid),
        .udp_rx_tready      (udp_rx.tready),
        .udp_rx_tlast       (udp_rx.tlast),
        .udp_rx_tuser       (udp_rx.tuser),
        .udp_rx_src_port    (udp_rx_src_port),
        .udp_rx_dst_port    (udp_rx_dst_port),
        .udp_tx_tdata       (udp_tx.tdata),
        .udp_tx_tvalid      (udp_tx.tvalid),
        .udp_tx_tready      (udp_tx.tready),
        .udp_tx_tlast       (udp_tx.tlast),
        .udp_tx_tuser       (udp_tx.tuser),
        .udp_tx_dst_mac     (udp_tx_dst_mac),
        .udp_tx_dst_ip      (udp_tx_dst_ip),
        .udp_tx_src_port    (udp_tx_src_port),
        .udp_tx_dst_port    (udp_tx_dst_port),
        .udp_tx_length      (udp_tx_length)
    );

    // ---- Market data parser --------------------------------
    // Consumes UDP RX byte stream, emits quote_t structs
    market_data_parser mkt_parser_inst (
        .clk         (clk),
        .rst         (rst),
        .udp_rx      (udp_rx),       // axis_if.slave — byte stream in
        .quote_out   (quote_out),    // quote_t — decoded quote
        .quote_valid (quote_valid)   // one-cycle pulse
    );

    // ---- Symbol router -------------------------------------
    // Routes quote to one of 4 order book slots by symbol_id
    symbol_router #(
        .N_BOOKS (4)
    ) sym_router_inst (
        .quote_in (quote_out),
        .book     (book_in)
    );

    // ---- Order books (4 symbols) ---------------------------
    generate
        for (genvar i = 0; i < 4; i++) begin : gen_order_books
            order_book #(
                .MAX_PRICE (1024)
            ) order_book_inst (
                .clk            (clk),
                .rst            (rst),
                .quote_in       (book_in[i]),
                .best_bid_price (best_bid_price[i]),
                .best_ask_price (best_ask_price[i]),
                .best_bid_size  (best_bid_size[i]),
                .best_ask_size  (best_ask_size[i])
            );
        end
    endgenerate

    // ---- UDP TX tie-off (stub — Phase 5 order engine) ------
    // When order engine is added it will drive these signals.
    // For now hold TX idle so udp_complete doesn't hang.
    assign udp_tx.tdata  = 8'h00;
    assign udp_tx.tvalid = 1'b0;
    assign udp_tx.tlast  = 1'b0;
    assign udp_tx.tuser  = 1'b0;
    assign udp_tx_dst_mac    = '0;
    assign udp_tx_dst_ip     = '0;
    assign udp_tx_src_port   = PORT_OUCH;
    assign udp_tx_dst_port   = '0;
    assign udp_tx_length     = '0;

    // ---- PHY reset -----------------------------------------
    phy_reset_ctrl #(
        .HOLD_CYCLES (PHY_RESET_CYCLES)
    ) phy_rst_inst (
        .clk        (clk),
        .rst        (rst),
        .phy_rst_n  (phy_rst_n)
    );

    // ---- MDIO stub -----------------------------------------
    assign eth_mdc  = 1'b0;
    assign eth_mdio = 1'bz;

    // ---- UART loopback (stub) ------------------------------
    assign uart_tx = uart_rx;

    // ---- Debug LEDs ----------------------------------------
    always_ff @(posedge clk) begin
        led[0] <= ~rst;              // on = running
        led[1] <= mac_rx.tvalid;    // RX activity
        led[2] <= quote_valid;      // market data being parsed
        led[3] <= mac_tx.tvalid;    // TX activity
    end

endmodule