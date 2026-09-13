// ============================================================
// mac_tx_arbiter.sv — 2:1 MAC TX AXI-Stream arbiter
//
// Port 0 (TCP orders) has priority over Port 1 (UDP telemetry).
// Once a packet starts it runs to tlast without preemption.
// Both input ports must hold valid/data/last stable until tready.
// ============================================================
`timescale 1ns/1ps
module mac_tx_arbiter (
    input  logic       clk,
    input  logic       rst_n,

    // Port 0: TCP orders (priority)
    input  logic [7:0] p0_tdata,
    input  logic       p0_tvalid,
    output logic       p0_tready,
    input  logic       p0_tlast,
    input  logic       p0_tuser,

    // Port 1: UDP telemetry (background)
    input  logic [7:0] p1_tdata,
    input  logic       p1_tvalid,
    output logic       p1_tready,
    input  logic       p1_tlast,
    input  logic       p1_tuser,

    // MAC TX output
    output logic [7:0] m_tdata,
    output logic       m_tvalid,
    input  logic       m_tready,
    output logic       m_tlast,
    output logic       m_tuser
);

    typedef enum logic [1:0] { IDLE, SEL0, SEL1 } state_t;
    state_t state;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            state <= IDLE;
        end else begin
            case (state)
                IDLE: begin
                    if      (p0_tvalid) state <= SEL0;
                    else if (p1_tvalid) state <= SEL1;
                end
                SEL0: if (p0_tvalid && p0_tlast && m_tready) state <= IDLE;
                SEL1: if (p1_tvalid && p1_tlast && m_tready)
                          state <= p0_tvalid ? SEL0 : IDLE;  // yield to p0 immediately
                default: state <= IDLE;
            endcase
        end
    end

    always_comb begin
        case (state)
            SEL0: begin
                m_tdata  = p0_tdata;
                m_tvalid = p0_tvalid;
                m_tlast  = p0_tlast;
                m_tuser  = p0_tuser;
                p0_tready = m_tready;
                p1_tready = 1'b0;
            end
            SEL1: begin
                m_tdata  = p1_tdata;
                m_tvalid = p1_tvalid;
                m_tlast  = p1_tlast;
                m_tuser  = p1_tuser;
                p1_tready = m_tready;
                p0_tready = 1'b0;
            end
            default: begin
                m_tdata  = 8'h00;
                m_tvalid = 1'b0;
                m_tlast  = 1'b0;
                m_tuser  = 1'b0;
                p0_tready = 1'b0;
                p1_tready = 1'b0;
            end
        endcase
    end

endmodule
