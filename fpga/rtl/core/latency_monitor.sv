// ============================================================
// latency_monitor.sv — Pipeline latency measurement (Phase 25)
//
// Measures the clock-cycle count between two pipeline events:
//   start: quote_in  — r_quote_valid crosses CDC into clk domain
//   stop:  order_out — strategy emits a new order (strat_valid)
//
// Tracks min / max / last measurement and sample count.
// All outputs are stable registers readable by telemetry_tx.
//
// Resolution: 1 clock cycle = 8 ns @ 125 MHz
// Max measurable: 2^32 cycles = ~34 seconds (more than enough)
//
// Clock domain: clk (125 MHz)
// ============================================================
`timescale 1ns/1ps
module latency_monitor (
    input  logic        clk,
    input  logic        rst,

    // Pipeline event inputs
    input  logic        quote_in,    // pulse: new quote arrived (r_quote_valid)
    input  logic        order_out,   // pulse: strategy placed an order (strat_valid)

    // Latency statistics outputs (clock cycles, 8 ns each)
    output logic [31:0] lat_min,     // minimum observed latency
    output logic [31:0] lat_max,     // maximum observed latency
    output logic [31:0] lat_last,    // most recent measurement
    output logic [31:0] lat_count    // number of measurements taken
);

    // Free-running cycle counter
    logic [31:0] cycle_cnt;
    always_ff @(posedge clk) begin
        if (rst) cycle_cnt <= '0;
        else     cycle_cnt <= cycle_cnt + 1'b1;
    end

    // Capture start time on quote_in
    logic        measuring;
    logic [31:0] t_start;

    always_ff @(posedge clk) begin
        if (rst) begin
            measuring  <= 1'b0;
            t_start    <= '0;
            lat_min    <= 32'hFFFF_FFFF;
            lat_max    <= '0;
            lat_last   <= '0;
            lat_count  <= '0;
        end else begin
            if (quote_in && !measuring) begin
                // Start a new measurement
                measuring <= 1'b1;
                t_start   <= cycle_cnt;
            end

            if (order_out && measuring) begin
                // Stop measurement, record delta
                logic [31:0] delta;
                delta = cycle_cnt - t_start;

                lat_last  <= delta;
                lat_count <= lat_count + 1'b1;

                if (delta < lat_min) lat_min <= delta;
                if (delta > lat_max) lat_max <= delta;

                measuring <= 1'b0;
            end
        end
    end

endmodule
