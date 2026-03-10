// ============================================================
// order_cdc_bridge.sv
// Clock domain crossing bridge for outgoing orders.
//
// Architecture:
//
//   clk domain                 │  rgmii_rxc domain
//   ──────────────────────     │  ──────────────────────
//   strategy                   │
//     order_out ─────────┐     │
//     order_valid ───────┤     │
//                         │     │
//                    encode_order()
//                         │     │
//                    itch_cdc_fifo (wclk=clk, rclk=rgmii_rxc)
//                                   │
//                            decode_order()
//                                   │
//                             order_out ────── ouch_encoder
//                             order_valid ────
//
// Drop policy: if FIFO full, order is silently dropped and
// w_drop_count increments. The cooldown in strategy.sv means
// this should essentially never happen.
//
// Reset:
//   wrst_n: active-low, clk domain
//   rrst_n: active-low, rgmii_rxc domain
// ============================================================

`timescale 1ns/1ps
module order_cdc_bridge
    import hft_pkg::*;
    import hft_msg_pkg::*;
(
    // ---- Write side: clk domain ----------------------------
    input  logic   wclk,
    input  logic   wrst_n,

    input  order_t w_order,
    input  logic   w_order_valid,

    output logic        w_drop,
    output logic [15:0] w_drop_count,

    // ---- Read side: rgmii_rxc domain -----------------------
    input  logic   rclk,
    input  logic   rrst_n,

    output order_t r_order,
    output logic   r_order_valid,

    // ---- Status (rclk domain) ------------------------------
    output logic   fifo_empty,
    output logic   fifo_full_rclk
);

    logic [ORDER_FIFO_WIDTH-1:0] fifo_wdata;
    logic [ORDER_FIFO_WIDTH-1:0] fifo_rdata;
    logic                        fifo_wren;
    logic                        fifo_rden;
    logic                        fifo_full;
    logic                        fifo_empty_raw;

    assign fifo_wdata = encode_order(w_order);
    assign fifo_wren  = w_order_valid && !fifo_full;

    // Drop detection (wclk domain)
    always_ff @(posedge wclk or negedge wrst_n) begin
        if (!wrst_n) begin
            w_drop       <= '0;
            w_drop_count <= '0;
        end else begin
            w_drop <= w_order_valid && fifo_full;
            if (w_order_valid && fifo_full)
                w_drop_count <= w_drop_count + 16'h1;
        end
    end

    itch_cdc_fifo #(
        .DATA_W (ORDER_FIFO_WIDTH),
        .DEPTH  (8)
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

    // Decode on read side (same FSM pattern as itch_msg_bridge)
    typedef enum logic { R_IDLE, R_OUTPUT } rstate_t;
    rstate_t rstate = R_IDLE;

    always_ff @(posedge rclk or negedge rrst_n) begin
        if (!rrst_n) begin
            rstate        <= R_IDLE;
            r_order_valid <= '0;
            r_order       <= '0;
            fifo_rden     <= '0;
        end else begin
            r_order_valid <= '0;
            fifo_rden     <= '0;

            case (rstate)
                R_IDLE: begin
                    if (!fifo_empty_raw) begin
                        fifo_rden <= 1'b1;
                        r_order   <= decode_order(fifo_rdata);
                        rstate    <= R_OUTPUT;
                    end
                end

                R_OUTPUT: begin
                    r_order_valid <= 1'b1;
                    rstate        <= R_IDLE;
                end

                default: rstate <= R_IDLE;
            endcase
        end
    end

    assign fifo_empty     = fifo_empty_raw;
    assign fifo_full_rclk = fifo_full;

endmodule
