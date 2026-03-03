// fpga/rtl/core/clk_rst.sv
module clk_rst (
    input  logic sys_clk,      // 100 MHz from board
    input  logic sys_rst_n,
    output logic clk,          // 125 MHz
    output logic clk90,        // 125 MHz, 90° shifted
    output logic clk200,       // 200 MHz
    output logic rst
);
    logic clk_fb;              // MMCM feedback — must loop back
    logic pll_locked;
    logic clk_unbuf;
    logic clk90_unbuf;
    logic clk200_unbuf;

    // ---- MMCM -----------------------------------------------
    // Input:  100 MHz  (DIVCLK_DIVIDE=1 → VCO input = 100 MHz)
    // VCO:    1000 MHz (CLKFBOUT_MULT_F=10 → stays in 600–1200 MHz range)
    // Outputs:
    //   CLKOUT0: 1000/8   = 125 MHz   (0°)
    //   CLKOUT1: 1000/8   = 125 MHz   (90°)
    //   CLKOUT2: 1000/5   = 200 MHz
    MMCME2_BASE #(
        .CLKIN1_PERIOD      (10.0),     // 100 MHz = 10ns
        .CLKFBOUT_MULT_F    (10.0),     // VCO = 100 * 10 = 1000 MHz
        .DIVCLK_DIVIDE      (1),
        .CLKOUT0_DIVIDE_F   (8.0),      // 125 MHz
        .CLKOUT0_PHASE      (0.0),
        .CLKOUT1_DIVIDE     (8),        // 125 MHz
        .CLKOUT1_PHASE      (90.0),     // 90° shifted
        .CLKOUT2_DIVIDE     (5),        // 200 MHz
        .CLKOUT2_PHASE      (0.0)
    ) mmcm_inst (
        .CLKIN1             (sys_clk),
        .CLKFBIN            (clk_fb),
        .CLKFBOUT           (clk_fb),   // feedback loopback
        .CLKOUT0            (clk_unbuf),
        .CLKOUT1            (clk90_unbuf),
        .CLKOUT2            (clk200_unbuf),
        .LOCKED             (pll_locked),
        .PWRDWN             (1'b0),
        .RST                (1'b0)
    );

    // ---- Global clock buffers ------------------------------
    // MMCM outputs must go through BUFGs before driving logic
    BUFG bufg_clk    (.I(clk_unbuf),    .O(clk));
    BUFG bufg_clk90  (.I(clk90_unbuf),  .O(clk90));
    BUFG bufg_clk200 (.I(clk200_unbuf), .O(clk200));

    // ---- Reset synchronizer --------------------------------
    reset_sync reset_sync_inst (
        .clk        (clk),
        .rst_in_n   (sys_rst_n & pll_locked),
        .rst_out    (rst)
    );

endmodule