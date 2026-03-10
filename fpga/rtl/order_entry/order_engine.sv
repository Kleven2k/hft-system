// ============================================================
// order_engine.sv — Top-level order entry wrapper
//
// Connects:
//   strategy (clk domain)
//     → order_cdc_bridge (clk → rgmii_rxc gray-code FIFO)
//       → ouch_encoder (rgmii_rxc domain, UDP AXI-S output)
//
// Instantiate in hft_top.sv; replace the UDP TX tie-off stubs.
// ============================================================
`timescale 1ns/1ps
module order_engine
    import hft_pkg::*;
#(
    parameter int N_BOOKS      = 4,
    parameter int SPREAD_MAX   = 20000,
    parameter int ORDER_QTY    = 100,
    parameter int COOLDOWN_CYC = 12_500_000
)(
    // ---- Strategy clock domain --------------------------------
    input  logic        clk,
    input  logic        rst,      // active-high
    input  logic        rst_n,    // active-low (for CDC write side)

    // Order book signals (clk domain)
    input  logic [31:0] best_bid_price [0:N_BOOKS-1],
    input  logic [31:0] best_ask_price [0:N_BOOKS-1],
    input  logic [31:0] spread         [0:N_BOOKS-1],
    input  logic        bid_valid      [0:N_BOOKS-1],
    input  logic        ask_valid      [0:N_BOOKS-1],

    // ---- Encoder clock domain ---------------------------------
    input  logic        enc_clk,   // rgmii_rxc
    input  logic        enc_rst_n, // active-low, rgmii_rxc domain

    // UDP TX AXI-Stream (rgmii_rxc domain)
    output logic [7:0]  udp_tx_tdata,
    output logic        udp_tx_tvalid,
    input  logic        udp_tx_tready,
    output logic        udp_tx_tlast,
    output logic        udp_tx_tuser,

    // UDP TX sideband (rgmii_rxc domain)
    output logic [47:0] udp_tx_dst_mac,
    output logic [31:0] udp_tx_dst_ip,
    output logic [15:0] udp_tx_src_port,
    output logic [15:0] udp_tx_dst_port,
    output logic [15:0] udp_tx_length,

    // Debug
    output logic [15:0] drop_count
);

    // ---- Strategy (clk domain) --------------------------------
    order_t strat_order;
    logic   strat_valid;

    strategy #(
        .N_BOOKS     (N_BOOKS),
        .SPREAD_MAX  (SPREAD_MAX),
        .ORDER_QTY   (ORDER_QTY),
        .COOLDOWN_CYC(COOLDOWN_CYC)
    ) u_strategy (
        .clk           (clk),
        .rst           (rst),
        .best_bid_price(best_bid_price),
        .best_ask_price(best_ask_price),
        .spread        (spread),
        .bid_valid     (bid_valid),
        .ask_valid     (ask_valid),
        .order_out     (strat_order),
        .order_valid   (strat_valid)
    );

    // ---- CDC bridge (clk → rgmii_rxc) -------------------------
    order_t enc_order;
    logic   enc_order_valid;

    order_cdc_bridge u_cdc (
        .wclk         (clk),
        .wrst_n       (rst_n),
        .w_order      (strat_order),
        .w_order_valid(strat_valid),
        .w_drop       (),
        .w_drop_count (drop_count),
        .rclk         (enc_clk),
        .rrst_n       (enc_rst_n),
        .r_order      (enc_order),
        .r_order_valid(enc_order_valid),
        .fifo_empty   (),
        .fifo_full_rclk()
    );

    // ---- OUCH encoder (rgmii_rxc domain) ----------------------
    ouch_encoder u_encoder (
        .clk         (enc_clk),
        .rst         (~enc_rst_n),
        .order_in    (enc_order),
        .order_valid (enc_order_valid),
        .tx_tdata    (udp_tx_tdata),
        .tx_tvalid   (udp_tx_tvalid),
        .tx_tready   (udp_tx_tready),
        .tx_tlast    (udp_tx_tlast),
        .tx_tuser    (udp_tx_tuser),
        .tx_dst_mac  (udp_tx_dst_mac),
        .tx_dst_ip   (udp_tx_dst_ip),
        .tx_src_port (udp_tx_src_port),
        .tx_dst_port (udp_tx_dst_port),
        .tx_length   (udp_tx_length)
    );

endmodule
