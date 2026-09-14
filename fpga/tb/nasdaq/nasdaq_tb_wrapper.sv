// ============================================================
// nasdaq_tb_wrapper.sv — Phase 31
// Flat single-slot wrapper around strategy.sv for replaying real
// NASDAQ tick data. Slots 1-3 are tied off (bid_valid=ask_valid=0).
//
// NASDAQ profile (validated in research/backtest/nasdaq_mm_backtest.py):
//   QUOTE_OFFSET=0 (quote at the touch), STALE_TICKS=2, SKEW_SHIFT=31 (off)
// STALE_MIN_TICKS lets stale_thresh=2 be represented even though
// QUOTE_OFFSET×STALE_MULT=0 at offset=0 (see strategy.sv).
// ============================================================
`timescale 1ns/1ps
module nasdaq_tb_wrapper
    import hft_pkg::*;
#(
    parameter int SPREAD_MAX      = 1_000_000,   // effectively unbounded — no NASDAQ EMA/spread cap in Python model
    parameter int COOLDOWN_CYC    = 1,           // nasdaq_mm_backtest.py has no timed cooldown —
                                                  // only "one order pending at a time" (enforced
                                                  // by strategy.sv's `pending` flag regardless).
                                                  // 1 cycle (not 0, to avoid a zero-width CD_W)
                                                  // is negligible next to the 4-16 cycle hold.
    parameter int TIMEOUT_CYC     = 2_000,       // safety-net only, must never be the dominant
                                                  // cancel path -- see note in test_nasdaq.py
                                                  // about MAX_HOLD_CYCLES compressing real time
    parameter int SKEW_SHIFT      = 31,          // inventory skew off — not part of validated NASDAQ strategy
    parameter int QUOTE_OFFSET    = 0,           // quote at the touch (winning param)
    parameter int STALE_MULT      = 2,
    parameter int STALE_MIN_TICKS = 2,           // decoupled floor — see strategy.sv
    parameter int MAX_POSITION    = 500,
    parameter int FAT_FINGER_BPS  = 2000,        // loose — not modeled in Python backtest
    parameter int MAX_BURST       = 1000,        // effectively unlimited — token bucket not in Python model
    parameter int REFILL_PERIOD   = 1
)(
    input  logic        clk,
    input  logic        rst,

    input  logic [31:0] best_bid_price_0,
    input  logic [31:0] best_ask_price_0,
    input  logic [31:0] mid_price_0,
    input  logic [31:0] spread_0,
    input  logic        bid_valid_0,
    input  logic        ask_valid_0,

    input  logic        kill_switch,

    input  logic        ack_valid,
    input  logic [63:0] ack_order_id,
    input  logic [7:0]  ack_status,
    input  logic [31:0] ack_fill_qty,

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
    logic [31:0] quote_offset_arr   [0:3];

    always_comb begin
        best_bid_price_arr[0] = best_bid_price_0;
        best_ask_price_arr[0] = best_ask_price_0;
        mid_price_arr[0]      = mid_price_0;
        spread_arr[0]         = spread_0;
        bid_valid_arr[0]      = bid_valid_0;
        ask_valid_arr[0]      = ask_valid_0;
        for (int i = 1; i < 4; i++) begin
            best_bid_price_arr[i] = 32'd0;
            best_ask_price_arr[i] = 32'd0;
            mid_price_arr[i]      = 32'd0;
            spread_arr[i]         = 32'd0;
            bid_valid_arr[i]      = 1'b0;
            ask_valid_arr[i]      = 1'b0;
        end
        for (int i = 0; i < 4; i++)
            quote_offset_arr[i] = 32'(QUOTE_OFFSET);
    end

    order_t order_out;

    logic signed [31:0] telem_pos_w  [0:3];
    logic signed [31:0] telem_pnl_w  [0:3];
    logic        [7:0]  telem_rej_w  [0:3];
    logic        [7:0]  telem_tok_w  [0:3];
    logic               telem_bv_w   [0:3];
    logic               telem_av_w   [0:3];
    logic        [63:0] telem_oid_w;

    strategy #(
        .N_BOOKS        (4),
        .SPREAD_MAX     (SPREAD_MAX),
        .ORDER_QTY      (100),
        .MAX_POSITION   (MAX_POSITION),
        .COOLDOWN_CYC   (COOLDOWN_CYC),
        .TIMEOUT_CYC    (TIMEOUT_CYC),
        .SKEW_SHIFT     (SKEW_SHIFT),
        .QUOTE_OFFSET   (QUOTE_OFFSET),
        .STALE_MULT     (STALE_MULT),
        .STALE_MIN_TICKS(STALE_MIN_TICKS),
        .FAT_FINGER_BPS (FAT_FINGER_BPS),
        .MAX_BURST      (MAX_BURST),
        .REFILL_PERIOD  (REFILL_PERIOD)
    ) u_dut (
        .clk           (clk),
        .rst           (rst),
        .best_bid_price(best_bid_price_arr),
        .best_ask_price(best_ask_price_arr),
        .mid_price     (mid_price_arr),
        .spread        (spread_arr),
        .bid_valid     (bid_valid_arr),
        .ask_valid     (ask_valid_arr),
        .quote_offset  (quote_offset_arr),
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
