// ============================================================
// strategy_tb_wrapper.sv
// Flat ports for cocotb — unpacked arrays → individual signals.
// Uses short COOLDOWN_CYC / REFILL_PERIOD so tests run quickly.
// TIMEOUT_CYC >> COOLDOWN_CYC so watchdog never fires in sims.
// ============================================================
`timescale 1ns/1ps
module strategy_tb_wrapper
    import hft_pkg::*;
#(
    parameter int SPREAD_MAX     = 20000,
    parameter int COOLDOWN_CYC   = 10,
    parameter int TIMEOUT_CYC    = 10_000,
    parameter int SKEW_SHIFT     = 0,    // inv_skew = position>>>0 = position (easy test math)
    parameter int FAT_FINGER_BPS = 500,
    parameter int MAX_BURST         = 5,
    parameter int REFILL_PERIOD     = 200,   // > 5×COOLDOWN so refill never fires during drain tests
    parameter int SPREAD_EMA_SHIFT  = 4
)(
    input  logic        clk,
    input  logic        rst,

    // Book 0
    input  logic [31:0] best_bid_price_0,
    input  logic [31:0] best_ask_price_0,
    input  logic [31:0] mid_price_0,
    input  logic [31:0] spread_0,
    input  logic        bid_valid_0,
    input  logic        ask_valid_0,

    // Book 1
    input  logic [31:0] best_bid_price_1,
    input  logic [31:0] best_ask_price_1,
    input  logic [31:0] mid_price_1,
    input  logic [31:0] spread_1,
    input  logic        bid_valid_1,
    input  logic        ask_valid_1,

    // Book 2
    input  logic [31:0] best_bid_price_2,
    input  logic [31:0] best_ask_price_2,
    input  logic [31:0] mid_price_2,
    input  logic [31:0] spread_2,
    input  logic        bid_valid_2,
    input  logic        ask_valid_2,

    // Book 3
    input  logic [31:0] best_bid_price_3,
    input  logic [31:0] best_ask_price_3,
    input  logic [31:0] mid_price_3,
    input  logic [31:0] spread_3,
    input  logic        bid_valid_3,
    input  logic        ask_valid_3,

    // Pre-trade risk (Phase 16)
    input  logic        kill_switch,

    // ACK inputs (Phase 15 OMS)
    input  logic        ack_valid,
    input  logic [63:0] ack_order_id,
    input  logic [7:0]  ack_status,
    input  logic [31:0] ack_fill_qty,

    // Flat order output
    output logic [63:0] order_id,
    output logic [31:0] order_price,
    output logic [31:0] order_qty,
    output logic [15:0] order_symbol_id,
    output logic [1:0]  order_side,
    output logic [1:0]  order_type,
    output logic        order_cancel,
    output logic        order_valid

);

    logic [31:0] best_bid_price_arr [0:3];
    logic [31:0] best_ask_price_arr [0:3];
    logic [31:0] mid_price_arr      [0:3];
    logic [31:0] spread_arr         [0:3];
    logic        bid_valid_arr      [0:3];
    logic        ask_valid_arr      [0:3];
    always_comb begin
        best_bid_price_arr[0] = best_bid_price_0;
        best_bid_price_arr[1] = best_bid_price_1;
        best_bid_price_arr[2] = best_bid_price_2;
        best_bid_price_arr[3] = best_bid_price_3;

        best_ask_price_arr[0] = best_ask_price_0;
        best_ask_price_arr[1] = best_ask_price_1;
        best_ask_price_arr[2] = best_ask_price_2;
        best_ask_price_arr[3] = best_ask_price_3;

        mid_price_arr[0] = mid_price_0;
        mid_price_arr[1] = mid_price_1;
        mid_price_arr[2] = mid_price_2;
        mid_price_arr[3] = mid_price_3;

        spread_arr[0] = spread_0;
        spread_arr[1] = spread_1;
        spread_arr[2] = spread_2;
        spread_arr[3] = spread_3;

        bid_valid_arr[0] = bid_valid_0;
        bid_valid_arr[1] = bid_valid_1;
        bid_valid_arr[2] = bid_valid_2;
        bid_valid_arr[3] = bid_valid_3;

        ask_valid_arr[0] = ask_valid_0;
        ask_valid_arr[1] = ask_valid_1;
        ask_valid_arr[2] = ask_valid_2;
        ask_valid_arr[3] = ask_valid_3;
    end

    order_t order_out;

    // Telemetry outputs — connected to DUT but not exposed to cocotb
    logic signed [31:0] telem_pos_w  [0:3];
    logic signed [31:0] telem_pnl_w  [0:3];
    logic        [7:0]  telem_rej_w  [0:3];
    logic        [7:0]  telem_tok_w  [0:3];
    logic               telem_bv_w   [0:3];
    logic               telem_av_w   [0:3];
    logic        [63:0] telem_oid_w;

    strategy #(
        .N_BOOKS       (4),
        .SPREAD_MAX    (SPREAD_MAX),
        .ORDER_QTY     (100),
        .COOLDOWN_CYC  (COOLDOWN_CYC),
        .TIMEOUT_CYC   (TIMEOUT_CYC),
        .SKEW_SHIFT    (SKEW_SHIFT),
        .FAT_FINGER_BPS  (FAT_FINGER_BPS),
        .MAX_BURST       (MAX_BURST),
        .REFILL_PERIOD   (REFILL_PERIOD),
        .SPREAD_EMA_SHIFT(SPREAD_EMA_SHIFT)
    ) u_dut (
        .clk           (clk),
        .rst           (rst),
        .best_bid_price(best_bid_price_arr),
        .best_ask_price(best_ask_price_arr),
        .mid_price     (mid_price_arr),
        .spread        (spread_arr),
        .bid_valid     (bid_valid_arr),
        .ask_valid     (ask_valid_arr),
        .kill_switch   (kill_switch),
        .ack_valid     (ack_valid),
        .ack_order_id  (ack_order_id),
        .ack_status    (ack_status),
        .ack_fill_qty  (ack_fill_qty),
        .order_out          (order_out),
        .order_valid        (order_valid),
        .telem_position     (telem_pos_w),
        .telem_pnl          (telem_pnl_w),
        .telem_reject_cnt   (telem_rej_w),
        .telem_token_cnt    (telem_tok_w),
        .telem_bid_valid    (telem_bv_w),
        .telem_ask_valid    (telem_av_w),
        .telem_order_id_cnt (telem_oid_w)
    );

    assign order_id        = order_out.order_id;
    assign order_price     = order_out.price;
    assign order_qty       = order_out.quantity;
    assign order_symbol_id = order_out.symbol_id;
    assign order_side      = order_out.side;
    assign order_type      = order_out.ord_type;
    assign order_cancel    = order_out.cancel;

endmodule
