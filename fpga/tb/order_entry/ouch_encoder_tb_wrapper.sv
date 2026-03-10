// ============================================================
// ouch_encoder_tb_wrapper.sv
// Flat ports for cocotb — unpacks order_t fields to individual signals.
// ============================================================
`timescale 1ns/1ps
module ouch_encoder_tb_wrapper
    import hft_pkg::*;
(
    input  logic        clk,
    input  logic        rst,

    // Flat order_t input
    input  logic [63:0] order_in_order_id,
    input  logic [31:0] order_in_price,
    input  logic [31:0] order_in_quantity,
    input  logic [15:0] order_in_symbol_id,
    input  logic [1:0]  order_in_side,
    input  logic [1:0]  order_in_ord_type,
    input  logic        order_in_valid,
    input  logic        order_valid,

    // AXI-Stream output
    output logic [7:0]  tx_tdata,
    output logic        tx_tvalid,
    input  logic        tx_tready,
    output logic        tx_tlast,
    output logic        tx_tuser,

    // UDP sideband
    output logic [47:0] tx_dst_mac,
    output logic [31:0] tx_dst_ip,
    output logic [15:0] tx_src_port,
    output logic [15:0] tx_dst_port,
    output logic [15:0] tx_length
);

    order_t order_in;
    always_comb begin
        order_in.order_id  = order_in_order_id;
        order_in.price     = order_in_price;
        order_in.quantity  = order_in_quantity;
        order_in.symbol_id = order_in_symbol_id;
        order_in.side      = order_side_t'(order_in_side);
        order_in.ord_type  = order_type_t'(order_in_ord_type);
        order_in.valid     = order_in_valid;
    end

    ouch_encoder u_dut (
        .clk        (clk),
        .rst        (rst),
        .order_in   (order_in),
        .order_valid(order_valid),
        .tx_tdata   (tx_tdata),
        .tx_tvalid  (tx_tvalid),
        .tx_tready  (tx_tready),
        .tx_tlast   (tx_tlast),
        .tx_tuser   (tx_tuser),
        .tx_dst_mac (tx_dst_mac),
        .tx_dst_ip  (tx_dst_ip),
        .tx_src_port(tx_src_port),
        .tx_dst_port(tx_dst_port),
        .tx_length  (tx_length)
    );

endmodule
