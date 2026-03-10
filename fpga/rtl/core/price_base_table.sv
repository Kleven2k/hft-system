// ============================================================
// price_base_table.sv — Per-symbol price base register file
//
// Provides one 32-bit base price per order book slot.
// price_idx = price[LB-1:0] - price_base[LB-1:0] maps the
// incoming price into the BRAM's 0..(MAX_LEVELS-1) index space.
//
// Write port: single-cycle write via wr_en/wr_slot/wr_base.
// Intended for future UART or AXI config path.
//
// Default values: parameterized at synthesis time.
// ============================================================
`timescale 1ns/1ps
module price_base_table
    import hft_pkg::*;
#(
    parameter int N_BOOKS        = 4,
    parameter int BASE0          = 100,
    parameter int BASE1          = 100,
    parameter int BASE2          = 100,
    parameter int BASE3          = 100
)(
    input  logic                          clk,
    input  logic                          rst,

    // Write port (clk domain)
    input  logic                          wr_en,
    input  logic [$clog2(N_BOOKS)-1:0]   wr_slot,
    input  logic [31:0]                   wr_base,

    // Read outputs (clk domain, registered)
    output logic [31:0] price_base [0:N_BOOKS-1]
);

    always_ff @(posedge clk) begin
        if (rst) begin
            price_base[0] <= 32'(BASE0);
            price_base[1] <= 32'(BASE1);
            price_base[2] <= 32'(BASE2);
            price_base[3] <= 32'(BASE3);
        end else if (wr_en) begin
            price_base[wr_slot] <= wr_base;
        end
    end

endmodule
