// ============================================================
// reset_sync.sv — Two-flop reset synchronizer
// Deasserts synchronously, asserts asynchronously.
// ============================================================
module reset_sync (
    input  logic clk,
    input  logic rst_in_n,   // async, active-low
    output logic rst_out     // sync, active-high
);
    logic [1:0] sync_ff;

    always_ff @(posedge clk or negedge rst_in_n) begin
        if (!rst_in_n)
            sync_ff <= 2'b11;
        else
            sync_ff <= {sync_ff[0], 1'b0};
    end

    assign rst_out = sync_ff[1];

endmodule