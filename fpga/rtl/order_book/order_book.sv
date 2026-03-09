// ============================================================
// order_book.sv  —  Single-symbol price-level order book
//
// BRAM-friendly architecture:
//   bid_book and ask_book are 256×32 true-dual-port BRAMs.
//   Write: on ADD/CANCEL/EXECUTE, RMW (read-modify-write) over
//          two cycles using a small pipeline.
//   Read:  best_bid_idx/best_ask_idx are registered pointers;
//          qty outputs read the BRAM at those addresses.
//
// Best-price pointers update in O(1) on ADD.
// On drain (level hits zero), a scan FSM finds the next best.
//
// Resource estimate (4 instances):
//   8 × RAMB18 (256×32), ~80 LUTs control logic per instance
// ============================================================
`timescale 1ns/1ps
module order_book
    import hft_pkg::*;
#(
    parameter int MAX_LEVELS = 256
)(
    input  logic        clk,
    input  logic        rst,

    input  quote_t      quote_in,
    input  logic        quote_valid,
    input  logic [31:0] price_base,

    output logic [31:0] best_bid_price,
    output logic [31:0] best_ask_price,
    output logic [31:0] best_bid_qty,
    output logic [31:0] best_ask_qty,
    output logic [31:0] spread,
    output logic [31:0] mid_price,
    output logic        bid_valid,
    output logic        ask_valid
);

    localparam int LB = $clog2(MAX_LEVELS);  // 8

    // ── BRAM arrays (inferred as RAMB18) ────────────────────
    // Simple-dual-port: one write port, one read port.
    // Read is registered (1-cycle latency).
    (* ram_style = "block" *)
    logic [31:0] bid_book [0:MAX_LEVELS-1];
    (* ram_style = "block" *)
    logic [31:0] ask_book [0:MAX_LEVELS-1];

    // ── Best-price pointers ──────────────────────────────────
    logic [LB-1:0] best_bid_idx;
    logic [LB-1:0] best_ask_idx;

    // ── RMW pipeline ────────────────────────────────────────
    // Cycle 0: latch incoming quote, read BRAM at price_idx
    // Cycle 1: compute new qty, write back to BRAM
    // Cycle 2: update best pointer if needed
    logic          rmw_valid_1, rmw_valid_2;
    logic [LB-1:0] rmw_idx_1,   rmw_idx_2;
    logic          rmw_is_bid_1, rmw_is_bid_2;
    logic [31:0]   rmw_shares_1;
    op_t           rmw_op_1,     rmw_op_2;
    logic [31:0]   rmw_old_qty;  // BRAM read result (1-cycle latency)
    logic [31:0]   rmw_new_qty;
    logic          rmw_drained;  // new_qty == 0

    logic [LB-1:0] price_idx;
    assign price_idx = quote_in.price[LB-1:0] - price_base[LB-1:0];

    // ── BRAM read (registered) ───────────────────────────────
    always_ff @(posedge clk) begin
        if (rmw_valid_1) begin
            if (rmw_is_bid_1)
                rmw_old_qty <= bid_book[rmw_idx_1];
            else
                rmw_old_qty <= ask_book[rmw_idx_1];
        end
    end

    // ── RMW stage 0→1: capture quote ────────────────────────
    always_ff @(posedge clk) begin
        if (rst) begin
            rmw_valid_1  <= 0;
        end else begin
            rmw_valid_1  <= quote_valid && quote_in.valid;
            rmw_idx_1    <= price_idx;
            rmw_is_bid_1 <= quote_in.is_bid;
            rmw_shares_1 <= quote_in.shares;
            rmw_op_1     <= quote_in.op;
        end
    end

    // ── RMW stage 1→2: compute + write ──────────────────────
    always_comb begin
        case (rmw_op_1)
            OP_ADD:
                rmw_new_qty = rmw_old_qty + rmw_shares_1;
            OP_CANCEL, OP_EXECUTE:
                rmw_new_qty = (rmw_old_qty >= rmw_shares_1)
                              ? rmw_old_qty - rmw_shares_1 : '0;
            default:
                rmw_new_qty = rmw_old_qty;
        endcase
    end
    assign rmw_drained = (rmw_new_qty == 0);

    always_ff @(posedge clk) begin
        rmw_valid_2  <= rmw_valid_1;
        rmw_idx_2    <= rmw_idx_1;
        rmw_is_bid_2 <= rmw_is_bid_1;
        rmw_op_2     <= rmw_op_1;

        if (rmw_valid_1) begin
            if (rmw_is_bid_1)
                bid_book[rmw_idx_1] <= rmw_new_qty;
            else
                ask_book[rmw_idx_1] <= rmw_new_qty;
        end
    end

    // ── Best-pointer update + scan FSM ───────────────────────
    typedef enum logic [1:0] { S_IDLE, S_SCAN_BID, S_SCAN_ASK } scan_t;
    scan_t         scan_state;
    logic [LB-1:0] scan_idx;

    always_ff @(posedge clk) begin
        if (rst) begin
            best_bid_idx <= '0;
            best_ask_idx <= '0;
            bid_valid    <= 0;
            ask_valid    <= 0;
            scan_state   <= S_IDLE;
            scan_idx     <= '0;
        end else begin

            // Stage-2 pointer update
            if (rmw_valid_2) begin
                if (rmw_is_bid_2) begin
                    if (rmw_op_2 == OP_ADD) begin
                        if (!bid_valid || rmw_idx_2 > best_bid_idx) begin
                            best_bid_idx <= rmw_idx_2;
                            bid_valid    <= 1;
                        end
                    end else if (rmw_drained && bid_valid
                                 && rmw_idx_2 == best_bid_idx) begin
                        bid_valid  <= 0;
                        scan_state <= S_SCAN_BID;
                        scan_idx   <= best_bid_idx - 1;
                    end
                end else begin
                    if (rmw_op_2 == OP_ADD) begin
                        if (!ask_valid || rmw_idx_2 < best_ask_idx) begin
                            best_ask_idx <= rmw_idx_2;
                            ask_valid    <= 1;
                        end
                    end else if (rmw_drained && ask_valid
                                 && rmw_idx_2 == best_ask_idx) begin
                        ask_valid  <= 0;
                        scan_state <= S_SCAN_ASK;
                        scan_idx   <= best_ask_idx + 1;
                    end
                end
            end

            // Scan FSM
            case (scan_state)
                S_SCAN_BID: begin
                    if (bid_book[scan_idx] > 0) begin
                        best_bid_idx <= scan_idx;
                        bid_valid    <= 1;
                        scan_state   <= S_IDLE;
                    end else if (scan_idx == 0)
                        scan_state <= S_IDLE;
                    else
                        scan_idx <= scan_idx - 1;
                end
                S_SCAN_ASK: begin
                    if (ask_book[scan_idx] > 0) begin
                        best_ask_idx <= scan_idx;
                        ask_valid    <= 1;
                        scan_state   <= S_IDLE;
                    end else if (scan_idx == MAX_LEVELS-1)
                        scan_state <= S_IDLE;
                    else
                        scan_idx <= scan_idx + 1;
                end
                default: ;
            endcase
        end
    end

    // ── Output assignments ───────────────────────────────────
    assign best_bid_price = bid_valid
                            ? price_base + {24'b0, best_bid_idx} : '0;
    assign best_ask_price = ask_valid
                            ? price_base + {24'b0, best_ask_idx} : '0;
    assign best_bid_qty   = bid_valid ? bid_book[best_bid_idx] : '0;
    assign best_ask_qty   = ask_valid ? ask_book[best_ask_idx] : '0;
    assign spread         = (bid_valid && ask_valid
                             && best_ask_price > best_bid_price)
                            ? best_ask_price - best_bid_price : '0;
    assign mid_price      = (bid_valid && ask_valid)
                            ? (best_bid_price + best_ask_price) >> 1 : '0;

endmodule