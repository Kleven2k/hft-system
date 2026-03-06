// ============================================================
// axis_if.sv — AXI-Stream interface
// Note: modports and assertions commented out for Icarus
// compatibility. Vivado synthesis uses full version.
// ============================================================
interface axis_if #(
    parameter int DATA_W = 8
) (
    input logic clk
);
    logic [DATA_W-1:0] tdata;
    logic              tvalid;
    logic              tready;
    logic              tlast;
    logic              tuser;

    modport master (
        output tdata, tvalid, tlast, tuser,
        input  tready,
        input  clk
    );

    modport slave (
        input  tdata, tvalid, tlast, tuser,
        output tready,
        input  clk
    );

    modport monitor (
        input tdata, tvalid, tready, tlast, tuser, clk
    );

endinterface