// ============================================================
// phy_reset_ctrl.sv
// Holds PHY in reset for HOLD_CYCLES after system reset,
// then releases. LAN8710 requires minimum 10ms.
// ============================================================
module phy_reset_ctrl #(
    parameter int HOLD_CYCLES = 1_250_000   // 10ms @ 125 MHz
) (
    input  logic clk,
    input  logic rst,
    output logic phy_rst_n
);
    logic [$clog2(HOLD_CYCLES+1)-1:0] count;

    always_ff @(posedge clk) begin
        if (rst) begin
            count     <= '0;
            phy_rst_n <= 1'b0;
        end else if (count < HOLD_CYCLES) begin
            count     <= count + 1;
            phy_rst_n <= 1'b0;
        end else begin
            phy_rst_n <= 1'b1;
        end
    end

endmodule