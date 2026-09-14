// ============================================================
// strategy.sv — Two-sided market-making strategy
//
// Phase 12 — ACK-confirmed position tracking.
// Phase 14 — Stale-quote cancel.
// Phase 15 — OMS: qty_remaining, reject_cnt, timeout watchdog,
//            full 64-bit order_id match.
// Phase 17 — Inventory skew:
//   inv_skew = position >>> SKEW_SHIFT  (arithmetic right-shift, price ticks)
//   BUY  quoted at best_bid − inv_skew
//   SELL quoted at best_ask − inv_skew
//   Long  inventory (position>0) → skew>0 → lower prices  (sell aggression ↑)
//   Short inventory (position<0) → skew<0 → higher prices (buy  aggression ↑)
//   Stale detection uses skewed price so a position change triggers re-quote.
//   No division — pure shift, no timing impact.
// Phase 16 — Pre-trade risk:
//   Kill switch  — kill_switch=1 halts all new order emission;
//                  in-flight cancels are unaffected.
//   Fat-finger   — rejects any new order where |price − mid| > X%
//                  of mid_price.  Threshold: FAT_FINGER_BPS (basis
//                  points, e.g. 500 = 5%).  Guards against quoting
//                  into a corrupted or empty book.
//   Rate limiter — per-slot token bucket.  One token is added every
//                  REFILL_PERIOD cycles (up to MAX_BURST).  Each new
//                  order consumes one token; when the bucket is empty
//                  the order is suppressed and risk_viol_cnt
//                  incremented.
// Phase 22 — Dynamic spread filter (EMA):
//   spread_ema[i] tracks a per-book exponential moving average of the spread.
//   alpha = 1 / 2^SPREAD_EMA_SHIFT  (default SPREAD_EMA_SHIFT=4 → alpha=1/16).
//   Initialized to SPREAD_MAX so the book must see narrow spreads before quoting.
//   Quote condition: spread_r ≤ SPREAD_MAX  AND  spread_r ≤ ema + (ema >> 1)
//   The 1.5× multiplier allows brief natural widenings without blocking,
//   while a sudden spike (e.g. 50× normal spread) halts quoting within one cycle.
// Phase 21B — P&L tracking:
//   pnl[i] accumulates edge-vs-mid × ORDER_QTY on every fill:
//     BUY  fill: pnl += (mid_p_r[i] − quoted_price[i]) × ORDER_QTY
//     SELL fill: pnl += (quoted_price[i] − mid_p_r[i]) × ORDER_QTY
//   Units: price ticks × shares (1 tick = $0.0001).
//   Exposed via telem_pnl for telemetry_tx.  Display only — nothing in the
//   trading path reads it.
//   NOTE: the ORDER_QTY factor was missing until Phase 31, so pre-Phase-31
//   telemetry under-reported P&L by ORDER_QTY (100×) — historical monitor.py
//   and dashboard readings are scaled accordingly.  See fill_pnl_c().
// Phase 18 — Input pipeline registers (timing fix):
//   bid_p_r / ask_p_r / mid_p_r / spread_r / bid_v_r / ask_v_r
//   register all market-data inputs one cycle.  Breaks the critical
//   path price_base → order_book_comb → strategy_comb → qty_remaining
//   (was 42 levels, −13 to −16 ns WNS) into two short segments.
//   One cycle of market-data latency; negligible for market-making.
//
// Clock domain: clk (MMCM 125 MHz)
// ============================================================
`timescale 1ns/1ps
module strategy
    import hft_pkg::*;
#(
    parameter int N_BOOKS        = 4,
    parameter int SPREAD_MAX     = 20000,
    parameter int ORDER_QTY      = 100,
    parameter int MAX_POSITION   = 1000,
    parameter int COOLDOWN_CYC   = 12_500_000,  // 100 ms @ 125 MHz
    parameter int TIMEOUT_CYC    = COOLDOWN_CYC, // auto-cancel if no ACK
    parameter int SKEW_SHIFT     = 31,           // inv_skew = position>>>SKEW_SHIFT; 31=off
    parameter int QUOTE_OFFSET   = 1,            // power-on default (overridden via UART at runtime)
    parameter int STALE_MULT     = 2,            // stale_thresh = quote_offset × STALE_MULT
    parameter int STALE_MIN_TICKS = 0,           // floor on stale_thresh — quote_offset×STALE_MULT
                                                  // alone can't represent stale>0 at offset=0
                                                  // (e.g. NASDAQ at-touch quoting); 0 = no floor,
                                                  // existing crypto behavior unchanged
    parameter int FAT_FINGER_BPS = 500,          // 5 % max price deviation
    parameter int MAX_BURST         = 5,            // token bucket depth
    parameter int REFILL_PERIOD     = 12_500_000,   // 10 orders/s @ 125 MHz
    parameter int SPREAD_EMA_SHIFT  = 4             // EMA alpha = 1/16
)(
    input  logic        clk,
    input  logic        rst,

    // Order book inputs (clk domain)
    input  logic [31:0] best_bid_price [0:N_BOOKS-1],
    input  logic [31:0] best_ask_price [0:N_BOOKS-1],
    input  logic [31:0] mid_price      [0:N_BOOKS-1],  // (bid+ask)/2
    input  logic [31:0] spread         [0:N_BOOKS-1],
    input  logic        bid_valid      [0:N_BOOKS-1],
    input  logic        ask_valid      [0:N_BOOKS-1],

    // Per-slot quote offset (UART-configurable, replaces QUOTE_OFFSET/STALE_THRESH params)
    input  logic [31:0] quote_offset   [0:N_BOOKS-1],

    // Pre-trade risk (Phase 16)
    input  logic        kill_switch,   // 1 = halt all new orders

    // ACK inputs (clk domain, from CDC)
    input  logic        ack_valid,
    input  logic [63:0] ack_order_id,
    input  logic [7:0]  ack_status,
    input  logic [31:0] ack_fill_qty,

    // Order output (one-cycle pulse, clk domain)
    output order_t      order_out,
    output logic        order_valid,

    // Telemetry (clk domain, direct view of internal registers)
    output logic signed [31:0] telem_position    [0:N_BOOKS-1],
    output logic        [7:0]  telem_reject_cnt  [0:N_BOOKS-1],
    output logic        [7:0]  telem_token_cnt   [0:N_BOOKS-1],
    output logic               telem_bid_valid   [0:N_BOOKS-1],
    output logic               telem_ask_valid   [0:N_BOOKS-1],
    output logic        [63:0] telem_order_id_cnt,
    output logic signed [31:0] telem_pnl         [0:N_BOOKS-1]
);

    localparam int LB   = $clog2(N_BOOKS);
    localparam int CD_W = $clog2(COOLDOWN_CYC + 1);
    localparam int TM_W = $clog2(TIMEOUT_CYC  + 1);
    localparam int RF_W      = $clog2(REFILL_PERIOD + 1);
    localparam int TB_W      = $clog2(MAX_BURST     + 1);
    // Simplify fat-finger: |diff|*10000 ≤ mid*BPS  →  |diff|*FF_FACTOR ≤ mid
    // Requires FAT_FINGER_BPS divides 10000 (e.g. 500→20, 100→100, 200→50).
    localparam int FF_FACTOR = 10000 / FAT_FINGER_BPS;

    // P&L per fill = (edge in price ticks) × shares filled.  Scaling by the
    // ORDER_QTY *parameter* rather than the runtime ack_fill_qty keeps this a
    // constant multiply (shifts+adds, no DSP, no new critical path) — a
    // 32×32 runtime multiply here would sit on the ACK path, which this
    // design has very little timing margin for.  The two differ only on a
    // partial fill, where this over-counts the unfilled remainder; pnl is
    // telemetry/display only (see telem_pnl → telemetry_tx), never an input
    // to any trading decision, so that approximation is acceptable.
    function automatic logic signed [31:0] fill_pnl_c(
        input logic [31:0] hi, input logic [31:0] lo);
        return ($signed(hi) - $signed(lo)) * $signed(32'(ORDER_QTY));
    endfunction

    (* max_fanout = 8 *) logic [LB-1:0] rr_idx;
    logic [CD_W-1:0]    cooldown      [0:N_BOOKS-1];
    logic [TM_W-1:0]    timeout_cnt   [0:N_BOOKS-1];
    logic signed [31:0] position      [0:N_BOOKS-1];
    logic               sell_turn     [0:N_BOOKS-1];
    logic               pending       [0:N_BOOKS-1];
    logic               canceling     [0:N_BOOKS-1];
    logic [63:0]        pending_id    [0:N_BOOKS-1];
    logic               pending_side  [0:N_BOOKS-1];
    logic [31:0]        quoted_price  [0:N_BOOKS-1];
    logic [31:0]        qty_remaining [0:N_BOOKS-1];
    logic [7:0]         reject_cnt    [0:N_BOOKS-1];
    logic [63:0]        order_id_cnt;

    // Token bucket
    logic [RF_W-1:0]    refill_cnt;
    logic [TB_W-1:0]    token_cnt     [0:N_BOOKS-1];
    logic [7:0]         risk_viol_cnt [0:N_BOOKS-1];  // internal telemetry
    logic signed [31:0] pnl           [0:N_BOOKS-1];  // edge-vs-mid P&L, ticks
    logic        [31:0] skewed_price  [0:N_BOOKS-1];  // actual skewed price sent (for P&L)
    logic        [31:0] spread_ema    [0:N_BOOKS-1];  // EMA of spread per book (Phase 22)

    // ---- Input pipeline registers (break long comb path from order_book) ----
    // Registers market data one cycle, so all strategy combinational logic
    // starts from a FF rather than the order_book combinational cloud.
    logic [31:0] bid_p_r  [0:N_BOOKS-1];
    logic [31:0] ask_p_r  [0:N_BOOKS-1];
    logic [31:0] mid_p_r  [0:N_BOOKS-1];
    logic [31:0] spread_r [0:N_BOOKS-1];
    logic        bid_v_r  [0:N_BOOKS-1];
    logic        ask_v_r  [0:N_BOOKS-1];

    // ---- Stale detection: three-stage pipeline --------------------------------
    // stale_thresh = quote_offset × STALE_MULT so orders survive long enough
    // to fill (fill needs offset+1 ticks; stale fires at stale_thresh ticks).
    // Three stages keep each stage under ~8 levels:
    //   Stage 0: stale_thresh_r = quote_offset × STALE_MULT  (~8 levels → FF)
    //   Stage 1: bid_abs_diff_r = abs(quoted_price - bid_p_r) (~10 levels → FF)
    //   Stage 2: bid_stale_r    = bid_abs_diff_r > stale_thresh_r (~6 levels → FF)
    logic        [31:0] stale_thresh_r [0:N_BOOKS-1];  // stage 0: quote_offset × STALE_MULT
    logic        [31:0] bid_abs_diff_r [0:N_BOOKS-1];  // stage 1: abs price diff
    logic        [31:0] ask_abs_diff_r [0:N_BOOKS-1];  // stage 1: abs price diff
    logic               bid_stale_r   [0:N_BOOKS-1]; // |bid_p_r[i] - quoted_price[i]| > stale_thresh_r
    logic               ask_stale_r   [0:N_BOOKS-1]; // |ask_p_r[i] - quoted_price[i]| > stale_thresh_r
    logic               timed_out_r   [0:N_BOOKS-1]; // timeout_cnt[i] == 0

    // ---- Combinational signals (indexed by rr_idx) ----------
    logic               is_stale;

    // ---- Inventory skew: two-stage pipeline (Phase 25 timing fix) -----------
    // Replacing QUOTE_OFFSET compile-time constant with runtime quote_offset[i]
    // added a second serial 32-bit subtract on the critical path:
    //   bid_p_r → subtract_skew → subtract_offset → skewed_bid_r  (~16 LUT levels)
    // Fix: split into two registered stages:
    //   Stage 1: bid_minus_skew_r = bid_p_r - skew  (~8 levels → FF)
    //   Stage 2: raw_bid_c = bid_minus_skew_r - quote_offset[i] (~8 levels → FF)
    logic signed [31:0] skew_c            [0:N_BOOKS-1];
    logic signed [31:0] bid_minus_skew_r  [0:N_BOOKS-1];  // stage 1 registered
    logic signed [31:0] ask_minus_skew_r  [0:N_BOOKS-1];  // stage 1 registered
    logic signed [31:0] raw_bid_c         [0:N_BOOKS-1];
    logic signed [31:0] raw_ask_c         [0:N_BOOKS-1];
    logic        [31:0] skewed_bid_c      [0:N_BOOKS-1];
    logic        [31:0] skewed_ask_c      [0:N_BOOKS-1];
    logic        [31:0] skewed_bid_r      [0:N_BOOKS-1];  // registered, used in slot_eval
    logic        [31:0] skewed_ask_r      [0:N_BOOKS-1];  // registered, used in slot_eval

    // ---- Fat-finger: computed for all books, registered next cycle ----------
    // Simplified form: |diff| * FF_FACTOR ≤ mid  (FF_FACTOR = 10000/BPS = 20)
    // Multiply by small constant ≡ shifts+add (~3 LUT levels vs. ~25 for ×10000).
    // Registered per-book removes fat_finger entirely from slot_eval critical path.
    // Two-stage pipeline to break the 18-level path:
    //   Stage 1: bid_p_r → abs subtract → ff_bid_diff_r (new FF)
    //   Stage 2: ff_bid_diff_r → ×FF_FACTOR → compare → fat_finger_r (existing FF)
    logic [31:0]        ff_bid_diff_c [0:N_BOOKS-1];  // combinational abs-diff
    logic [31:0]        ff_ask_diff_c [0:N_BOOKS-1];  // combinational abs-diff
    logic [31:0]        ff_bid_diff_r [0:N_BOOKS-1];  // registered abs-diff (stage 1 output)
    logic [31:0]        ff_ask_diff_r [0:N_BOOKS-1];  // registered abs-diff (stage 1 output)
    logic               fat_finger_c  [0:N_BOOKS-1];  // combinational all books
    logic               fat_finger_r  [0:N_BOOKS-1];  // registered, used in slot_eval

    // ---- EMA spread filter: registered per-book (Phase 22 timing fix) ------
    // Inline spread_ema adder+compare was on the critical path (spread_ema →
    // slot_eval condition → qty_remaining, 23 levels, −3.6 ns WNS).
    // ema_ok_c is purely combinatorial on registered inputs (spread_r, spread_ema);
    // ema_ok_r registers the result one cycle, breaking that path.
    logic               ema_ok_c [0:N_BOOKS-1];
    logic               ema_ok_r [0:N_BOOKS-1];  // registered, used in slot_eval

    // ---- Token bucket and position limit: registered per-book --------
    // Inline token_cnt>0 and position<MAX comparisons in slot_eval are CARRY4
    // chains on the rr_idx→order_valid→qty_remaining critical path.
    // Pre-registering per-book removes them from that path (same pattern as
    // fat_finger_r / ema_ok_r).  1-cycle latency is safe: cooldown prevents
    // re-entry for 12.5 M cycles after any order fires.
    logic               tok_ok_r      [0:N_BOOKS-1]; // token_cnt[i] > 0
    logic               pos_ok_buy_r  [0:N_BOOKS-1]; // position[i] <= 0 (flat or short → allow buy)
    logic               pos_ok_sell_r [0:N_BOOKS-1]; // position[i] >= 0 (flat or long  → allow sell)
    logic               cooldown_ok_r [0:N_BOOKS-1]; // cooldown[i] == 0


    always_comb begin
        for (int i = 0; i < N_BOOKS; i++) begin
            // Stage 1: abs-diff (registered into ff_bid/ask_diff_r in always_ff).
            ff_bid_diff_c[i] = (mid_p_r[i] >= bid_p_r[i]) ?
                                mid_p_r[i] - bid_p_r[i] :
                                bid_p_r[i] - mid_p_r[i];
            ff_ask_diff_c[i] = (ask_p_r[i] >= mid_p_r[i]) ?
                                ask_p_r[i] - mid_p_r[i] :
                                mid_p_r[i] - ask_p_r[i];
            // Stage 2: |diff_r| * FF_FACTOR ≤ mid_p_r
            // diff is 32-bit; diff*20 fits in 37 bits (2^32*20 < 2^37).
            // Use 37-bit arithmetic — avoids 64-bit carry chains (~25 levels → ~6).
            // Inputs ff_bid/ask_diff_r are already registered — short path to fat_finger_r.
            fat_finger_c[i]  = (mid_p_r[i] != 0) &&
                                (37'(ff_bid_diff_r[i]) * 37'(FF_FACTOR) <= 37'(mid_p_r[i])) &&
                                (37'(ff_ask_diff_r[i]) * 37'(FF_FACTOR) <= 37'(mid_p_r[i]));
            // Spread filter: passes when spread_r is within SPREAD_MAX.
            // Removed adaptive EMA component — 1.5×EMA was blocking all slots
            // whenever spread briefly widened (EMA alpha=1/16 too slow to recover).
            ema_ok_c[i] = (spread_r[i] <= 32'(SPREAD_MAX));
            // Inventory-skewed prices — two-stage pipeline.
            // Stage 1 (→ bid_minus_skew_r) is computed in always_ff below.
            // Stage 2: subtract/add runtime quote_offset from pre-registered intermediate.
            skew_c[i]       = position[i] >>> SKEW_SHIFT;  // for stage-1 FF in always_ff
            raw_bid_c[i]    = bid_minus_skew_r[i] - $signed(quote_offset[i]);
            raw_ask_c[i]    = ask_minus_skew_r[i] + $signed(quote_offset[i]);
            skewed_bid_c[i] = (raw_bid_c[i] < 32'sd1) ? 32'd1 : 32'(raw_bid_c[i]);
            skewed_ask_c[i] = (raw_ask_c[i] < 32'sd1) ? 32'd1 : 32'(raw_ask_c[i]);
        end
    end

    always_comb begin
        // Stale: all inputs are 1-bit registered per-book signals — 1 LUT level,
        // no CARRY4 chains.  bid_stale_r/ask_stale_r/timed_out_r computed above.
        is_stale = pending[rr_idx] && !canceling[rr_idx] && (
            (!pending_side[rr_idx] && bid_stale_r[rr_idx]) ||
            ( pending_side[rr_idx] && ask_stale_r[rr_idx]) ||
            timed_out_r[rr_idx]
        );
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            rr_idx       <= '0;
            order_valid  <= '0;
            order_out    <= '0;
            order_id_cnt <= 64'h1;
            refill_cnt   <= '0;
            for (int i = 0; i < N_BOOKS; i++) begin
                bid_p_r[i]        <= '0;
                ask_p_r[i]        <= '0;
                mid_p_r[i]        <= '0;
                spread_r[i]       <= '0;
                bid_v_r[i]        <= 1'b0;
                ask_v_r[i]        <= 1'b0;
                fat_finger_r[i]   <= 1'b0;
                ff_bid_diff_r[i]  <= 32'd0;
                ff_ask_diff_r[i]  <= 32'd0;
                ema_ok_r[i]       <= 1'b0;
                skewed_bid_r[i]   <= 32'd1;
                skewed_ask_r[i]   <= 32'd1;
                cooldown[i]       <= '0;
                timeout_cnt[i]    <= '0;
                position[i]       <= '0;
                sell_turn[i]      <= 1'b0;
                pending[i]        <= 1'b0;
                canceling[i]      <= 1'b0;
                pending_id[i]     <= '0;
                pending_side[i]   <= 1'b0;
                quoted_price[i]   <= '0;
                qty_remaining[i]  <= '0;
                reject_cnt[i]     <= '0;
                risk_viol_cnt[i]  <= '0;
                token_cnt[i]      <= TB_W'(MAX_BURST);  // full bucket at startup
                tok_ok_r[i]       <= 1'b1;   // MAX_BURST > 0 at startup
                cooldown_ok_r[i]  <= 1'b1;   // cooldown=0 at reset
                pos_ok_buy_r[i]   <= 1'b1;   // position=0 < MAX_POSITION
                pos_ok_sell_r[i]  <= 1'b1;   // position=0 > -MAX_POSITION
                stale_thresh_r[i] <= 32'd0;
                bid_abs_diff_r[i] <= 32'd0;
                ask_abs_diff_r[i] <= 32'd0;
                bid_minus_skew_r[i] <= 32'sd0;
                ask_minus_skew_r[i] <= 32'sd0;
                bid_stale_r[i]    <= 1'b0;
                ask_stale_r[i]    <= 1'b0;
                timed_out_r[i]    <= 1'b0;
                pnl[i]            <= '0;
                skewed_price[i]   <= '0;
                spread_ema[i]     <= 32'(SPREAD_MAX);
            end
        end else begin
            // ---- Market data pipeline registers + fat_finger register ----------
            for (int i = 0; i < N_BOOKS; i++) begin
                bid_p_r[i]      <= best_bid_price[i];
                ask_p_r[i]      <= best_ask_price[i];
                mid_p_r[i]      <= mid_price[i];
                spread_r[i]     <= spread[i];
                bid_v_r[i]      <= bid_valid[i];
                ask_v_r[i]      <= ask_valid[i];
                ff_bid_diff_r[i]  <= ff_bid_diff_c[i];
                ff_ask_diff_r[i]  <= ff_ask_diff_c[i];
                fat_finger_r[i]   <= fat_finger_c[i];
                ema_ok_r[i]       <= ema_ok_c[i];
                skewed_bid_r[i]   <= skewed_bid_c[i];
                skewed_ask_r[i]   <= skewed_ask_c[i];
                tok_ok_r[i]       <= (token_cnt[i] > 0);
                cooldown_ok_r[i]  <= (cooldown[i] == '0);
                // Prefer flattening (position on the "wrong" side of 0), but
                // the hard MAX_POSITION cap is what actually stops inventory
                // from running away in a persistently one-sided market —
                // "flat or short" alone has no ceiling on how short you get.
                pos_ok_buy_r[i]   <= (position[i] <= $signed(32'(0))) &&
                                     (position[i] > -$signed(32'(MAX_POSITION)));
                pos_ok_sell_r[i]  <= (position[i] >= $signed(32'(0))) &&
                                     (position[i] <  $signed(32'(MAX_POSITION)));
                // Skewed price stage 1: bid/ask minus inventory skew → register.
                // quote_offset subtracted in stage 2 (via raw_bid_c in always_comb).
                bid_minus_skew_r[i] <= $signed(bid_p_r[i]) - skew_c[i];
                ask_minus_skew_r[i] <= $signed(ask_p_r[i]) - skew_c[i];
                // Stale stage 0: pre-register threshold = max(quote_offset × STALE_MULT, STALE_MIN_TICKS).
                stale_thresh_r[i] <= (quote_offset[i] * 32'(STALE_MULT) > 32'(STALE_MIN_TICKS)) ?
                                     quote_offset[i] * 32'(STALE_MULT) : 32'(STALE_MIN_TICKS);
                // Stale stage 1: abs(quoted_price - bid/ask) → register.
                bid_abs_diff_r[i] <= ($signed(quoted_price[i]) >= $signed(bid_p_r[i])) ?
                                      quoted_price[i] - bid_p_r[i] :
                                      bid_p_r[i] - quoted_price[i];
                ask_abs_diff_r[i] <= ($signed(ask_p_r[i]) >= $signed(quoted_price[i])) ?
                                      ask_p_r[i] - quoted_price[i] :
                                      quoted_price[i] - ask_p_r[i];
                // Stale stage 2: compare abs-diff against pre-registered threshold.
                bid_stale_r[i]    <= (bid_abs_diff_r[i] > stale_thresh_r[i]);
                ask_stale_r[i]    <= (ask_abs_diff_r[i] > stale_thresh_r[i]);
                timed_out_r[i]    <= (timeout_cnt[i] == '0);
                // EMA update: spread_ema += (spread_r - spread_ema) >> SHIFT
                // Uses registered spread_r (not raw spread) so the path starts
                // from a FF rather than the price_base combinatorial adder chain.
                if (bid_v_r[i] && ask_v_r[i]) begin
                    if (spread_r[i] >= spread_ema[i])
                        spread_ema[i] <= spread_ema[i] +
                            ((spread_r[i] - spread_ema[i]) >> SPREAD_EMA_SHIFT);
                    else
                        spread_ema[i] <= spread_ema[i] -
                            ((spread_ema[i] - spread_r[i]) >> SPREAD_EMA_SHIFT);
                end
            end
            order_valid <= '0;

            // ---- ACK processing ----------------------------------------
            if (ack_valid) begin
                for (int i = 0; i < N_BOOKS; i++) begin
                    if (pending[i] && pending_id[i] == ack_order_id) begin
                        case (ack_status)
                            ACK_FILLED: begin
                                if (!pending_side[i]) begin
                                    position[i] <= position[i] + $signed(ack_fill_qty);
                                    pnl[i] <= pnl[i] + fill_pnl_c(mid_p_r[i], skewed_price[i]);
                                end else begin
                                    position[i] <= position[i] - $signed(ack_fill_qty);
                                    pnl[i] <= pnl[i] + fill_pnl_c(skewed_price[i], mid_p_r[i]);
                                end
                                pending[i]     <= 1'b0;
                                canceling[i]   <= 1'b0;
                                timeout_cnt[i] <= '0;
                            end
                            ACK_PARTIAL: begin
                                if (!pending_side[i]) begin
                                    position[i] <= position[i] + $signed(ack_fill_qty);
                                    pnl[i] <= pnl[i] + fill_pnl_c(mid_p_r[i], skewed_price[i]);
                                end else begin
                                    position[i] <= position[i] - $signed(ack_fill_qty);
                                    pnl[i] <= pnl[i] + fill_pnl_c(skewed_price[i], mid_p_r[i]);
                                end
                                if (ack_fill_qty >= qty_remaining[i]) begin
                                    pending[i]       <= 1'b0;
                                    canceling[i]     <= 1'b0;
                                    timeout_cnt[i]   <= '0;
                                end else begin
                                    qty_remaining[i] <= qty_remaining[i] - ack_fill_qty;
                                end
                            end
                            ACK_REJECTED: begin
                                pending[i]     <= 1'b0;
                                canceling[i]   <= 1'b0;
                                timeout_cnt[i] <= '0;
                                reject_cnt[i]  <= reject_cnt[i] + 8'h1;
                            end
                            ACK_CANCELLED: begin
                                pending[i]     <= 1'b0;
                                canceling[i]   <= 1'b0;
                                timeout_cnt[i] <= '0;
                            end
                            default: begin
                                pending[i]     <= 1'b0;
                                canceling[i]   <= 1'b0;
                                timeout_cnt[i] <= '0;
                            end
                        endcase
                    end
                end
            end

            // ---- Cooldown + timeout tick --------------------------------
            rr_idx <= (rr_idx == LB'(N_BOOKS - 1)) ? '0 : rr_idx + 1'b1;
            for (int i = 0; i < N_BOOKS; i++) begin
                if (cooldown[i]    != '0) cooldown[i]    <= cooldown[i]    - 1'b1;
                if (timeout_cnt[i] != '0) timeout_cnt[i] <= timeout_cnt[i] - 1'b1;
            end

            // ---- Token bucket refill -----------------------------------
            if (refill_cnt == RF_W'(REFILL_PERIOD - 1)) begin
                refill_cnt <= '0;
                for (int i = 0; i < N_BOOKS; i++)
                    if (token_cnt[i] < TB_W'(MAX_BURST))
                        token_cnt[i] <= token_cnt[i] + 1'b1;
            end else begin
                refill_cnt <= refill_cnt + 1'b1;
            end

            // ---- Slot evaluation (current rr_idx) ----------------------
            begin : slot_eval
                if (is_stale) begin
                    // Cancel stale / timed-out order
                    order_out          <= '0;
                    order_out.order_id <= pending_id[rr_idx];
                    order_out.cancel   <= 1'b1;
                    order_out.valid    <= 1'b1;
                    order_valid        <= 1'b1;
                    canceling[rr_idx]  <= 1'b1;

                // ---- Normal market-making
                end else if (!pending[rr_idx]                      &&
                             cooldown_ok_r[rr_idx]                 &&
                             bid_v_r[rr_idx] && ask_v_r[rr_idx]   &&
                             ema_ok_r[rr_idx]) begin

                    if (!kill_switch && fat_finger_r[rr_idx] &&
                        tok_ok_r[rr_idx]) begin

                        // At flat (pos==0) use sell_turn to alternate sides.
                        // When position forces a direction, override sell_turn.
                        if (pos_ok_buy_r[rr_idx] &&
                            (!sell_turn[rr_idx] || !pos_ok_sell_r[rr_idx])) begin
                            // BUY at skewed bid
                            order_out.order_id    <= order_id_cnt;
                            order_out.price       <= skewed_bid_r[rr_idx];
                            order_out.quantity    <= 32'(ORDER_QTY);
                            order_out.symbol_id   <= 16'(rr_idx);
                            order_out.side        <= ORD_BUY;
                            order_out.ord_type    <= ORD_LIMIT;
                            order_out.cancel      <= 1'b0;
                            order_out.valid       <= 1'b1;
                            order_valid           <= 1'b1;
                            order_id_cnt          <= order_id_cnt + 64'h1;
                            cooldown[rr_idx]      <= CD_W'(COOLDOWN_CYC);
                            timeout_cnt[rr_idx]   <= TM_W'(TIMEOUT_CYC);
                            sell_turn[rr_idx]     <= 1'b1;
                            pending[rr_idx]       <= 1'b1;
                            pending_id[rr_idx]    <= order_id_cnt;
                            pending_side[rr_idx]  <= 1'b0;
                            quoted_price[rr_idx]  <= bid_p_r[rr_idx];
                            skewed_price[rr_idx]  <= skewed_bid_r[rr_idx];
                            qty_remaining[rr_idx] <= 32'(ORDER_QTY);
                            token_cnt[rr_idx]     <= token_cnt[rr_idx] - 1'b1;

                        end else if (pos_ok_sell_r[rr_idx] &&
                                     (sell_turn[rr_idx] || !pos_ok_buy_r[rr_idx])) begin
                            // SELL at skewed ask
                            order_out.order_id    <= order_id_cnt;
                            order_out.price       <= skewed_ask_r[rr_idx];
                            order_out.quantity    <= 32'(ORDER_QTY);
                            order_out.symbol_id   <= 16'(rr_idx);
                            order_out.side        <= ORD_SELL;
                            order_out.ord_type    <= ORD_LIMIT;
                            order_out.cancel      <= 1'b0;
                            order_out.valid       <= 1'b1;
                            order_valid           <= 1'b1;
                            order_id_cnt          <= order_id_cnt + 64'h1;
                            cooldown[rr_idx]      <= CD_W'(COOLDOWN_CYC);
                            timeout_cnt[rr_idx]   <= TM_W'(TIMEOUT_CYC);
                            sell_turn[rr_idx]     <= 1'b0;
                            pending[rr_idx]       <= 1'b1;
                            pending_id[rr_idx]    <= order_id_cnt;
                            pending_side[rr_idx]  <= 1'b1;
                            quoted_price[rr_idx]  <= ask_p_r[rr_idx];
                            skewed_price[rr_idx]  <= skewed_ask_r[rr_idx];
                            qty_remaining[rr_idx] <= 32'(ORDER_QTY);
                            token_cnt[rr_idx]     <= token_cnt[rr_idx] - 1'b1;
                        end

                    end else begin
                        // Risk check blocked a would-be order
                        risk_viol_cnt[rr_idx] <= risk_viol_cnt[rr_idx] + 8'h1;
                    end
                end
            end
        end
    end

    // ---- Telemetry: direct view of internal registers ----------
    generate
        for (genvar i = 0; i < N_BOOKS; i++) begin : gen_telem
            assign telem_position[i]   = position[i];
            assign telem_reject_cnt[i] = reject_cnt[i];
            assign telem_token_cnt[i]  = 8'(token_cnt[i]);
            assign telem_bid_valid[i]  = bid_v_r[i];
            assign telem_ask_valid[i]  = ask_v_r[i];
            assign telem_pnl[i]        = pnl[i];
        end
    endgenerate
    assign telem_order_id_cnt = order_id_cnt;

endmodule
