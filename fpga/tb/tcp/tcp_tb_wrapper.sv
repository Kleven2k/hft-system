// ============================================================
// tcp_tb_wrapper.sv — Phase 27 cocotb testbench wrapper
//
// Instantiates tcp_engine + soup_session together.
// Timeouts reduced for fast simulation.
// ============================================================
`timescale 1ns/1ps
module tcp_tb_wrapper
    import hft_pkg::*;
#(
    // Use tiny timeouts so tests don't take millions of cycles.
    // ARP_TIMEOUT=200, SYN_TIMEOUT=200, RECONNECT=400
    parameter [31:0] ARP_TIMEOUT = 32'd200,
    parameter [31:0] SYN_TIMEOUT = 32'd200,
    parameter [31:0] RECONNECT   = 32'd400,
    parameter int    HB_CYC      = 500
)(
    input  logic        clk,
    input  logic        rst_n,

    // MAC RX (feed from cocotb — simulating frames arriving from network)
    input  logic [7:0]  mac_rx_tdata,
    input  logic        mac_rx_tvalid,
    output logic        mac_rx_tready,
    input  logic        mac_rx_tlast,
    input  logic        mac_rx_tuser,

    // MAC TX (capture in cocotb — DUT frames sent to network)
    output logic [7:0]  mac_tx_tdata,
    output logic        mac_tx_tvalid,
    input  logic        mac_tx_tready,
    output logic        mac_tx_tlast,
    output logic        mac_tx_tuser,

    // OUCH input — drive from cocotb to simulate order_engine output
    input  logic [7:0]  ouch_tdata,
    input  logic        ouch_tvalid,
    output logic        ouch_tready,
    input  logic        ouch_tlast,

    // ACK output — capture in cocotb
    output logic        ack_valid,
    output logic [63:0] ack_order_id,
    output logic [7:0]  ack_status,
    output logic [31:0] ack_fill_qty,

    // Status
    output logic        connected,
    output logic        session_active,
    output logic [15:0] drop_count
);

    // TCP ↔ SOUP streams
    logic [7:0] app_tx_tdata;
    logic       app_tx_tvalid, app_tx_tready, app_tx_tlast;
    logic [7:0] app_rx_tdata;
    logic       app_rx_tvalid, app_rx_tready, app_rx_tlast;

    tcp_engine #(
        .ARP_TIMEOUT (ARP_TIMEOUT),
        .SYN_TIMEOUT (SYN_TIMEOUT),
        .RECONNECT   (RECONNECT)
    ) u_tcp (
        .clk              (clk),
        .rst_n            (rst_n),
        .mac_rx_tdata     (mac_rx_tdata),
        .mac_rx_tvalid    (mac_rx_tvalid),
        .mac_rx_tready    (mac_rx_tready),
        .mac_rx_tlast     (mac_rx_tlast),
        .mac_rx_tuser     (mac_rx_tuser),
        .mac_tx_tdata     (mac_tx_tdata),
        .mac_tx_tvalid    (mac_tx_tvalid),
        .mac_tx_tready    (mac_tx_tready),
        .mac_tx_tlast     (mac_tx_tlast),
        .mac_tx_tuser     (mac_tx_tuser),
        .app_tx_tdata     (app_tx_tdata),
        .app_tx_tvalid    (app_tx_tvalid),
        .app_tx_tready    (app_tx_tready),
        .app_tx_tlast     (app_tx_tlast),
        .app_rx_tdata     (app_rx_tdata),
        .app_rx_tvalid    (app_rx_tvalid),
        .app_rx_tready    (app_rx_tready),
        .app_rx_tlast     (app_rx_tlast),
        .connected        (connected)
    );

    soup_session #(
        .HB_CYC (HB_CYC)
    ) u_soup (
        .clk              (clk),
        .rst_n            (rst_n),
        .tcp_connected    (connected),
        .app_tx_tdata     (app_tx_tdata),
        .app_tx_tvalid    (app_tx_tvalid),
        .app_tx_tready    (app_tx_tready),
        .app_tx_tlast     (app_tx_tlast),
        .app_rx_tdata     (app_rx_tdata),
        .app_rx_tvalid    (app_rx_tvalid),
        .app_rx_tready    (app_rx_tready),
        .app_rx_tlast     (app_rx_tlast),
        .ouch_tdata       (ouch_tdata),
        .ouch_tvalid      (ouch_tvalid),
        .ouch_tready      (ouch_tready),
        .ouch_tlast       (ouch_tlast),
        .ack_valid        (ack_valid),
        .ack_order_id     (ack_order_id),
        .ack_status       (ack_status),
        .ack_fill_qty     (ack_fill_qty),
        .session_active   (session_active),
        .drop_count       (drop_count)
    );

endmodule
