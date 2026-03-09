// ============================================================
// order_book_tb_wrapper.sv
// Flat ports for cocotb — unpacks quote_t to individual signals.
// ============================================================
`timescale 1ns/1ps
module order_book_tb_wrapper
    import hft_pkg::*;
(
    input  logic        clk,
    input  logic        rst,

    // Flat quote input
    input  logic        quote_valid,
    input  logic        quote_is_bid,
    input  logic [31:0] quote_price,
    input  logic [31:0] quote_shares,
    input  logic [15:0] quote_symbol_id,
    input  logic [1:0]  quote_op,        // 0=ADD 1=CANCEL 2=EXECUTE
    input  logic [63:0] quote_timestamp,

    // Price base
    input  logic [31:0] price_base,

    // Outputs
    output logic [31:0] best_bid_price,
    output logic [31:0] best_ask_price,
    output logic [31:0] best_bid_qty,
    output logic [31:0] best_ask_qty,
    output logic [31:0] spread,
    output logic [31:0] mid_price,
    output logic        bid_valid,
    output logic        ask_valid
);

    quote_t q;
    always_comb begin
        q.valid     = quote_valid;
        q.is_bid    = quote_is_bid;
        q.price     = quote_price;
        q.shares    = quote_shares;
        q.symbol_id = quote_symbol_id;
        q.op        = op_t'(quote_op);
        q.timestamp = quote_timestamp;
    end

    order_book #(.MAX_LEVELS(256)) u_dut (
        .clk           (clk),
        .rst           (rst),
        .quote_in      (q),
        .quote_valid   (quote_valid),
        .price_base    (price_base),
        .best_bid_price(best_bid_price),
        .best_ask_price(best_ask_price),
        .best_bid_qty  (best_bid_qty),
        .best_ask_qty  (best_ask_qty),
        .spread        (spread),
        .mid_price     (mid_price),
        .bid_valid     (bid_valid),
        .ask_valid     (ask_valid)
    );

endmodule