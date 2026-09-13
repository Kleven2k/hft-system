// ============================================================
// itch_msg_bridge.sv
// Clock domain crossing bridge for ITCH parsed quotes.
//
// Instantiate this between market_data_parser and order_book.
//
// Architecture:
//
//   rgmii_rxc domain          │  clk_unbuf domain
//   ─────────────────────     │  ──────────────────────
//   market_data_parser        │
//     quote_out ──────────┐   │
//     quote_valid ────────┤   │
//                         │   │
//                    encode_quote()
//                         │   │
//                    itch_cdc_fifo (wclk=rgmii_rxc, rclk=clk_unbuf)
//                                 │
//                          decode_quote()
//                                 │
//                           quote_out ────── order_book
//                           quote_valid ────
//
// Backpressure:
//   If FIFO is full (very unlikely at 1 msg/packet rate),
//   the incoming quote is DROPPED and drop_count increments.
//   This should never happen in practice — log it in simulation.
//
// Reset:
//   wrst_n is asserted from the rgmii_rxc domain reset.
//   rrst_n is asserted from the clk_unbuf domain reset.
//   Both resets are active-low.
//   Pass sys_rst_n (active-low version of sys_rst) for rrst_n,
//   and a synchronized version for wrst_n.
// ============================================================

`timescale 1ns/1ps
module itch_msg_bridge
    import hft_pkg::*;
    import hft_msg_pkg::*;
(
    // ---- Write side: rgmii_rxc domain ----------------------
    input  logic   wclk,          // rgmii_rxc (125 MHz, PHY-derived)
    input  logic   wrst_n,        // active-low reset, wclk domain

    input  quote_t w_quote,       // from market_data_parser
    input  logic   w_quote_valid, // one-cycle pulse

    output logic   w_drop,        // pulse: quote dropped (FIFO full)
    output logic [15:0] w_drop_count,  // sticky counter

    // ---- Read side: clk_unbuf domain -----------------------
    input  logic   rclk,          // clk_unbuf (125 MHz, MMCM)
    input  logic   rrst_n,        // active-low reset, rclk domain

    output quote_t r_quote,       // to order_book / symbol_router
    output logic   r_quote_valid, // one-cycle pulse

    // ---- Status (rclk domain) ------------------------------
    output logic   fifo_empty,
    output logic   fifo_full_rclk
);

    // ---- Internal signals ----------------------------------
    logic [FIFO_WIDTH-1:0] fifo_wdata;
    logic [FIFO_WIDTH-1:0] fifo_rdata;
    logic                  fifo_wren;
    logic                  fifo_rden;
    logic                  fifo_full;
    logic                  fifo_empty_raw;

    // ---- Encode on write side ------------------------------
    assign fifo_wdata = encode_quote(w_quote);
    assign fifo_wren  = w_quote_valid && !fifo_full;

    // ---- Drop detection (wclk domain) ----------------------
    always_ff @(posedge wclk or negedge wrst_n) begin
        if (!wrst_n) begin
            w_drop       <= '0;
            w_drop_count <= '0;
        end else begin
            w_drop <= w_quote_valid && fifo_full;
            if (w_quote_valid && fifo_full)
                w_drop_count <= w_drop_count + 1;
        end
    end

    // ---- FIFO instantiation --------------------------------
    itch_cdc_fifo #(
        .DATA_W (FIFO_WIDTH),
        .DEPTH  (16)           // 16 entries is plenty: ~1 msg per UDP packet
    ) u_fifo (
        .wclk    (wclk),
        .wrst_n  (wrst_n),
        .wren    (fifo_wren),
        .wdata   (fifo_wdata),
        .wr_full (fifo_full),

        .rclk    (rclk),
        .rrst_n  (rrst_n),
        .rden    (fifo_rden),
        .rdata   (fifo_rdata),
        .rd_empty(fifo_empty_raw)
    );

    // ---- Decode on read side -------------------------------
    // rdata is COMBINATIONAL: mem[rptr] always visible.
    // Asserting rden increments rptr on the NEXT clock edge.
    // So: capture rdata the same cycle rden is asserted (before ptr moves).
    //
    // One-cycle read pipeline:
    //   Cycle 0 (R_IDLE):   fifo not empty → assert rden, capture rdata → R_OUTPUT
    //   Cycle 1 (R_OUTPUT): pulse r_quote_valid, go back to R_IDLE
    typedef enum logic { R_IDLE, R_OUTPUT } rstate_t;
    rstate_t rstate = R_IDLE;

    always_ff @(posedge rclk or negedge rrst_n) begin
        if (!rrst_n) begin
            rstate        <= R_IDLE;
            r_quote_valid <= '0;
            r_quote       <= '0;
            fifo_rden     <= '0;
        end else begin
            r_quote_valid <= '0;   // default
            fifo_rden     <= '0;   // default

            case (rstate)
                R_IDLE: begin
                    if (!fifo_empty_raw) begin
                        fifo_rden         <= '1;            // advance pointer next cycle
                        // Inline decode: avoids Icarus packed-struct function-return-value
                        // bug where r_quote <= decode_quote(...) zeroes non-valid fields.
                        r_quote.valid     <= 1'b1;
                        r_quote.op        <= op_t'(fifo_rdata[146:145]);
                        r_quote.is_bid    <= fifo_rdata[144];
                        r_quote.timestamp <= fifo_rdata[143:80];
                        r_quote.price     <= fifo_rdata[79:48];
                        r_quote.shares    <= fifo_rdata[47:16];
                        r_quote.symbol_id <= fifo_rdata[15:0];
                        rstate            <= R_OUTPUT;
                    end
                end

                R_OUTPUT: begin
                    r_quote_valid <= '1;                    // pulse valid
                    rstate        <= R_IDLE;
                end

                default: rstate <= R_IDLE;
            endcase
        end
    end

    // ---- Status outputs ------------------------------------
    assign fifo_empty     = fifo_empty_raw;
    assign fifo_full_rclk = fifo_full;   // NOTE: fifo_full is wclk domain
                                          // Only use for debug/monitoring,
                                          // not for combinational logic in rclk

endmodule