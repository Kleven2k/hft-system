// ============================================================
// itch_msg_bridge_tb_wrapper.sv
// Unpacks quote_t structs to flat ports so cocotb can drive
// and sample individual fields without needing SV struct support.
// ============================================================

`timescale 1ns/1ps
module itch_msg_bridge_tb_wrapper
    import hft_pkg::*;
    import hft_msg_pkg::*;
(
    // ---- wclk domain ----------------------------------------
    input  logic        wclk,
    input  logic        wrst_n,

    // Flat quote input (cocotb drives these)
    input  logic        w_quote_valid,
    input  logic [63:0] w_quote_timestamp,
    input  logic [31:0] w_quote_price,
    input  logic [31:0] w_quote_shares,
    input  logic [15:0] w_quote_symbol_id,
    input  logic        w_quote_is_bid,

    output logic        w_drop,
    output logic [15:0] w_drop_count,

    // ---- rclk domain ----------------------------------------
    input  logic        rclk,
    input  logic        rrst_n,

    // Flat quote output (cocotb reads these)
    output logic        r_quote_valid,
    output logic [63:0] r_quote_timestamp,
    output logic [31:0] r_quote_price,
    output logic [31:0] r_quote_shares,
    output logic [15:0] r_quote_symbol_id,
    output logic        r_quote_is_bid,

    output logic        fifo_empty,
    output logic        fifo_full_rclk
);

    // ---- Pack flat inputs → quote_t ------------------------
    quote_t w_quote;
    always_comb begin
        w_quote.valid     = w_quote_valid;
        w_quote.timestamp = w_quote_timestamp;
        w_quote.price     = w_quote_price;
        w_quote.shares    = w_quote_shares;
        w_quote.symbol_id = w_quote_symbol_id;
        w_quote.is_bid    = w_quote_is_bid;
    end

    // ---- DUT -----------------------------------------------
    quote_t r_quote_struct;

    itch_msg_bridge u_dut (
        .wclk           (wclk),
        .wrst_n         (wrst_n),
        .w_quote        (w_quote),
        .w_quote_valid  (w_quote_valid),
        .w_drop         (w_drop),
        .w_drop_count   (w_drop_count),

        .rclk           (rclk),
        .rrst_n         (rrst_n),
        .r_quote        (r_quote_struct),
        .r_quote_valid  (r_quote_valid),

        .fifo_empty     (fifo_empty),
        .fifo_full_rclk (fifo_full_rclk)
    );

    // ---- Unpack quote_t → flat outputs ---------------------
    assign r_quote_timestamp = r_quote_struct.timestamp;
    assign r_quote_price     = r_quote_struct.price;
    assign r_quote_shares    = r_quote_struct.shares;
    assign r_quote_symbol_id = r_quote_struct.symbol_id;
    assign r_quote_is_bid    = r_quote_struct.is_bid;

endmodule