// ============================================================
// itch_cdc_fifo.sv
// Asynchronous FIFO for clock domain crossing.
//
// Write side: rgmii_rxc domain (ITCH parser output)
// Read  side: clk_unbuf domain (order book input)
//
// Implementation: dual-clock FIFO with gray-code pointers.
// Gray-code ensures only 1 bit changes per pointer increment,
// making the 2-FF synchronizer safe (no multi-bit metastability).
//
// Parameters:
//   DATA_W  — data width (use hft_msg_pkg::FIFO_WIDTH = 145)
//   DEPTH   — must be power of 2 (default 16 entries)
//
// Timing:
//   Write latency: 1 cycle (data available next wclk)
//   Read latency:  1 cycle after rden asserted
//   Full flag:     combinational, registered copy also available
//   Empty flag:    combinational, safe to read when ~empty
//
// Safety:
//   - Never write when full  (caller must check wr_full)
//   - Never read  when empty (caller must check rd_empty)
//   - FIFO memory is implemented as a simple array
//     (Vivado infers distributed RAM for small depths)
// ============================================================

`timescale 1ns/1ps
module itch_cdc_fifo #(
    parameter int DATA_W = 145,
    parameter int DEPTH  = 16    // must be power of 2
) (
    // ---- Write port (rgmii_rxc domain) ---------------------
    input  logic              wclk,
    input  logic              wrst_n,       // async reset, sync release
    input  logic              wren,
    input  logic [DATA_W-1:0] wdata,
    output logic              wr_full,

    // ---- Read port (clk_unbuf domain) ----------------------
    input  logic              rclk,
    input  logic              rrst_n,       // async reset, sync release
    input  logic              rden,
    output logic [DATA_W-1:0] rdata,
    output logic              rd_empty
);

    localparam int PTR_W = $clog2(DEPTH) + 1;  // +1 for full/empty disambiguation

    // ---- Memory --------------------------------------------
    logic [DATA_W-1:0] mem [0:DEPTH-1];

    // ---- Write domain pointers (binary + gray) -------------
    logic [PTR_W-1:0] wptr_bin  = '0;
    logic [PTR_W-1:0] wptr_gray = '0;
    logic [PTR_W-1:0] wptr_gray_sync1 = '0;   // synchronized into rclk
    logic [PTR_W-1:0] wptr_gray_sync2 = '0;

    // ---- Read domain pointers (binary + gray) --------------
    logic [PTR_W-1:0] rptr_bin  = '0;
    logic [PTR_W-1:0] rptr_gray = '0;
    logic [PTR_W-1:0] rptr_gray_sync1 = '0;   // synchronized into wclk
    logic [PTR_W-1:0] rptr_gray_sync2 = '0;

    // ---- Binary to Gray conversion -------------------------
    function automatic logic [PTR_W-1:0] bin2gray(input logic [PTR_W-1:0] b);
        return b ^ (b >> 1);
    endfunction

    // ---- Gray to Binary conversion -------------------------
    function automatic logic [PTR_W-1:0] gray2bin(input logic [PTR_W-1:0] g);
        logic [PTR_W-1:0] b;
        b[PTR_W-1] = g[PTR_W-1];
        for (int i = PTR_W-2; i >= 0; i--)
            b[i] = b[i+1] ^ g[i];
        return b;
    endfunction

    // ---- Write logic (wclk domain) -------------------------
    always_ff @(posedge wclk or negedge wrst_n) begin
        if (!wrst_n) begin
            wptr_bin  <= '0;
            wptr_gray <= '0;
        end else begin
            if (wren && !wr_full) begin
                mem[wptr_bin[PTR_W-2:0]] <= wdata;
                wptr_bin  <= wptr_bin + 1;
                wptr_gray <= bin2gray(wptr_bin + 1);
            end
        end
    end

    // ---- Synchronize rptr_gray into wclk ------------------
    always_ff @(posedge wclk or negedge wrst_n) begin
        if (!wrst_n) begin
            rptr_gray_sync1 <= '0;
            rptr_gray_sync2 <= '0;
        end else begin
            rptr_gray_sync1 <= rptr_gray;
            rptr_gray_sync2 <= rptr_gray_sync1;
        end
    end

    // Full when write ptr is one full revolution ahead of read ptr
    // In gray code: MSB differs, next MSB differs, rest same
    assign wr_full = (wptr_gray == {~rptr_gray_sync2[PTR_W-1:PTR_W-2],
                                     rptr_gray_sync2[PTR_W-3:0]});

    // ---- Read logic (rclk domain) --------------------------
    always_ff @(posedge rclk or negedge rrst_n) begin
        if (!rrst_n) begin
            rptr_bin  <= '0;
            rptr_gray <= '0;
        end else begin
            if (rden && !rd_empty) begin
                rptr_bin  <= rptr_bin + 1;
                rptr_gray <= bin2gray(rptr_bin + 1);
            end
        end
    end

    // ---- Synchronize wptr_gray into rclk ------------------
    always_ff @(posedge rclk or negedge rrst_n) begin
        if (!rrst_n) begin
            wptr_gray_sync1 <= '0;
            wptr_gray_sync2 <= '0;
        end else begin
            wptr_gray_sync1 <= wptr_gray;
            wptr_gray_sync2 <= wptr_gray_sync1;
        end
    end

    // Empty when read ptr has caught up with write ptr
    assign rd_empty = (rptr_gray == wptr_gray_sync2);

    // ---- Read data output ----------------------------------
    assign rdata = mem[rptr_bin[PTR_W-2:0]];

endmodule