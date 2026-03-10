// ============================================================
// strategy.sv — Tight-spread limit-order strategy
//
// Monitors N_BOOKS order books in round-robin (one book per clk).
// When a book shows a spread <= SPREAD_MAX and both sides are
// valid, emits a limit-buy order at the best bid price.
//
// Per-book cooldown prevents re-entry until COOLDOWN_CYC clocks
// have elapsed since the last order on that slot.
//
// Clock domain: clk (MMCM 125 MHz)
// ============================================================
`timescale 1ns/1ps
module strategy
    import hft_pkg::*;
#(
    parameter int N_BOOKS     = 4,
    parameter int SPREAD_MAX  = 20000,    // trigger if spread <= this (price units)
    parameter int ORDER_QTY   = 100,      // shares per order
    parameter int COOLDOWN_CYC = 12_500_000 // 100 ms @ 125 MHz between orders per slot
)(
    input  logic        clk,
    input  logic        rst,

    // Order book inputs (clk domain)
    input  logic [31:0] best_bid_price [0:N_BOOKS-1],
    input  logic [31:0] best_ask_price [0:N_BOOKS-1],
    input  logic [31:0] spread         [0:N_BOOKS-1],
    input  logic        bid_valid      [0:N_BOOKS-1],
    input  logic        ask_valid      [0:N_BOOKS-1],

    // Order output (one-cycle pulse, clk domain)
    output order_t      order_out,
    output logic        order_valid
);

    localparam int LB = $clog2(N_BOOKS);
    localparam int CD_W = $clog2(COOLDOWN_CYC + 1);

    logic [LB-1:0] rr_idx;
    logic [CD_W-1:0] cooldown [0:N_BOOKS-1];
    logic [63:0] order_id_cnt;

    always_ff @(posedge clk) begin
        if (rst) begin
            rr_idx       <= '0;
            order_valid  <= '0;
            order_out    <= '0;
            order_id_cnt <= 64'h1;
            for (int i = 0; i < N_BOOKS; i++) cooldown[i] <= '0;
        end else begin
            order_valid <= '0;

            // Advance round-robin
            rr_idx <= (rr_idx == LB'(N_BOOKS - 1)) ? '0 : rr_idx + 1'b1;

            // Tick down active cooldowns
            for (int i = 0; i < N_BOOKS; i++) begin
                if (cooldown[i] != '0) cooldown[i] <= cooldown[i] - 1'b1;
            end

            // Evaluate current slot
            if (bid_valid[rr_idx] && ask_valid[rr_idx] &&
                spread[rr_idx] <= SPREAD_MAX &&
                cooldown[rr_idx] == '0) begin

                order_out.order_id  <= order_id_cnt;
                order_out.price     <= best_bid_price[rr_idx]; // limit buy at best bid
                order_out.quantity  <= 32'(ORDER_QTY);
                order_out.symbol_id <= 16'(rr_idx);
                order_out.side      <= ORD_BUY;
                order_out.ord_type  <= ORD_LIMIT;
                order_out.valid     <= 1'b1;
                order_valid         <= 1'b1;
                order_id_cnt        <= order_id_cnt + 64'h1;
                cooldown[rr_idx]    <= CD_W'(COOLDOWN_CYC);
            end
        end
    end

endmodule
