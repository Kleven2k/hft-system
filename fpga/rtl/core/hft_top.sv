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
    output logic        mdc,
    inout  wire         mdio,
    output logic        phy_rst_n,

    // Debug
    input  logic        uart_rx,
    output logic        uart_tx,
    output logic [3:0]  led
);

    // ---- Clocks & reset ------------------------------------
    logic clk;       // 125 MHz
    logic clk90;     // 125 MHz, 90° shifted
    logic clk200;    // 200 MHz — for IDELAYCTRL only
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
    // Required by the RGMII RX input delay taps (IDELAY2 primitives
    // inside verilog-ethernet's rgmii_phy_if.v). Must be in the same
    // clock region as the RGMII RX pins. Driven by 200 MHz ref clock.
    // If Vivado reports "no IDELAYCTRL in region", check that clk200
    // is routed to the correct bank via a BUFG.
    (* IODELAY_GROUP = "rgmii_idelay" *)
    IDELAYCTRL idelayctrl_inst (
        .REFCLK (clk200),
        .RST    (rst),
        .RDY    ()          // optionally gate logic on this
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
        .rgmii_rxc          (rgmii_rxc),
        .rgmii_rxd          (rgmii_rxd),
        .rgmii_rx_ctl       (rgmii_rx_ctl),
        .rgmii_txc          (rgmii_txc),
        .rgmii_txd          (rgmii_txd),
        .rgmii_tx_ctl       (rgmii_tx_ctl),
        .rx_axis_tdata      (mac_rx.tdata),
        .rx_axis_tvalid     (mac_rx.tvalid),
        .rx_axis_tready     (mac_rx.tready),
        .rx_axis_tlast      (mac_rx.tlast),
        .rx_axis_tuser      (mac_rx.tuser),
        .tx_axis_tdata      (mac_tx.tdata),
        .tx_axis_tvalid     (mac_tx.tvalid),
        .tx_axis_tready     (mac_tx.tready),
        .tx_axis_tlast      (mac_tx.tlast),
        .tx_axis_tuser      (mac_tx.tuser),
        .ifg_delay          (8'd12),
        .speed              ()
    );

    // ---- Ethernet stack (ARP/IP/UDP) -----------------------
    eth_stack_wrapper eth_stack_inst (
        .clk                (clk),
        .rst                (rst),
        .mac_rx             (mac_rx),
        .mac_tx             (mac_tx),
        .udp_rx             (udp_rx),
        .udp_rx_src_port    (udp_rx_src_port),
        .udp_rx_dst_port    (udp_rx_dst_port),
        .udp_tx             (udp_tx),
        .udp_tx_dst_mac     (udp_tx_dst_mac),
        .udp_tx_dst_ip      (udp_tx_dst_ip),
        .udp_tx_src_port    (udp_tx_src_port),
        .udp_tx_dst_port    (udp_tx_dst_port),
        .udp_tx_length      (udp_tx_length)
    );

    // ---- Market data parser (stub — Phase 4) ---------------
    // market_data_parser mkt_parser_inst ( ... );

    // ---- Order engine (stub — Phase 5) ---------------------
    // order_engine ord_eng_inst ( ... );

    // ---- PHY reset -----------------------------------------
    phy_reset_ctrl #(
        .HOLD_CYCLES (PHY_RESET_CYCLES)
    ) phy_rst_inst (
        .clk        (clk),
        .rst        (rst),
        .phy_rst_n  (phy_rst_n)
    );

    // ---- MDIO stub -----------------------------------------
    assign mdc  = 1'b0;
    assign mdio = 1'bz;     // hi-Z until MDIO controller added

    // ---- UART loopback (stub) ------------------------------
    assign uart_tx = uart_rx;

    // ---- Debug LEDs ----------------------------------------
    always_ff @(posedge clk) begin
        led[0] <= ~rst;              // on = running
        led[1] <= mac_rx.tvalid;    // RX activity
        led[2] <= mac_tx.tvalid;    // TX activity
        led[3] <= 1'b0;             // reserved
    end

endmodule