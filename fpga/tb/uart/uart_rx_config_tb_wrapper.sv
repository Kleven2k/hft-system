// ============================================================
// uart_rx_config_tb_wrapper.sv — cocotb wrapper
// Exposes uart_rx_config with an overrideable baud divisor
// so simulation runs fast (BAUD_DIV_TB=40, not 1085).
// ============================================================
`timescale 1ns/1ps
module uart_rx_config_tb_wrapper
    import hft_pkg::*;
#(
    parameter int N_BOOKS     = 4,
    parameter int BAUD_DIV_TB = 40    // fast sim: 40 cycles/bit instead of 1085
)(
    input  logic        clk,
    input  logic        rst,

    input  logic        uart_rx,

    output logic        wr_en,
    output logic [1:0]  wr_slot,
    output logic [31:0] wr_base
);

    uart_rx_config #(
        .N_BOOKS     (N_BOOKS),
        .BAUD_DIV_TB (BAUD_DIV_TB)
    ) dut (
        .clk     (clk),
        .rst     (rst),
        .uart_rx (uart_rx),
        .wr_en   (wr_en),
        .wr_slot (wr_slot),
        .wr_base (wr_base)
    );

endmodule
