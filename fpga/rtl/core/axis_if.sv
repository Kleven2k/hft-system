// ============================================================
// axis_if.sv — AXI-Stream interface
// Replaces the 5-port copy-paste on every module boundary.
//
// Usage:
//   axis_if #(.DATA_W(8)) my_bus (.clk(clk));
//
//   module foo (axis_if.master tx, axis_if.slave rx);
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
    logic              tuser;   // error flag (per verilog-ethernet convention)

    // Master drives data out
    modport master (
        output tdata, tvalid, tlast, tuser,
        input  tready,
        input  clk
    );

    // Slave receives data
    modport slave (
        input  tdata, tvalid, tlast, tuser,
        output tready,
        input  clk
    );

    // Passive monitor — for testbenches only, no drive
    modport monitor (
        input tdata, tvalid, tready, tlast, tuser, clk
    );

    // ---- Protocol assertions (active in simulation) --------
    // Once tvalid is asserted, tdata/tlast must be held stable
    // until tready — this is the AXI-Stream handshake rule.
    // synthesis translate_off
    property valid_stable;
        @(posedge clk) (tvalid && !tready) |=> $stable(tdata) && $stable(tlast);
    endproperty
    assert property (valid_stable)
        else $error("AXIS VIOLATION: tdata/tlast changed while tvalid && !tready");

    // tuser (error) should only be asserted on tlast
    property user_on_last;
        @(posedge clk) (tvalid && tuser) |-> tlast;
    endproperty
    assert property (user_on_last)
        else $warning("AXIS WARNING: tuser asserted without tlast");
    // synthesis translate_on

endinterface