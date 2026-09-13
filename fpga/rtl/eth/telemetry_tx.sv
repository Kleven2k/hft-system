// ============================================================
// telemetry_tx.sv — UDP telemetry transmitter (Phase 20/21B/25)
//
// Every TELEM_PERIOD cycles, snapshots strategy state and
// streams an 80-byte UDP payload on the AXI-S interface.
// Runs in clk domain; caller provides axis_async_fifo to
// cross to rgmii_rxc (enc_clk) before the AXI arbiter.
//
// Packet layout (big-endian, 80 bytes):
//   [  0- 3]  magic  0x48465401  ('HFT\x01')
//   [  4- 7]  seq_num (uint32, wraps)
//   [  8-15]  telem_order_id_cnt (uint64)
//   [ 16-27]  slot 0: pos(4) pnl(4) reject(1) token(1) flags(1) pad(1) = 12B
//   [ 28-39]  slot 1
//   [ 40-51]  slot 2
//   [ 52-63]  slot 3
//     flags byte: bit0=bid_valid  bit1=ask_valid
//     pnl: signed int32, edge-vs-mid in price ticks (1 tick = $0.0001)
//          positive = capturing spread, negative = paying to unwind inventory
//   --- Phase 25: latency stats (bytes 64-79) ---
//   [ 64-67]  lat_min   (uint32, clock cycles, 8 ns each)
//   [ 68-71]  lat_max   (uint32, clock cycles)
//   [ 72-75]  lat_last  (uint32, clock cycles)
//   [ 76-79]  lat_count (uint32, number of measurements)
//
// Clock domain: clk (125 MHz)
// ============================================================
`timescale 1ns/1ps
module telemetry_tx
    import hft_pkg::*;
#(
    parameter int     N_BOOKS      = 4,
    parameter int     TELEM_PERIOD = CLK_FREQ_HZ  // 1 s @ 125 MHz
)(
    input  logic        clk,
    input  logic        rst,

    // Strategy telemetry inputs (clk domain)
    input  logic signed [31:0] position    [0:N_BOOKS-1],
    input  logic signed [31:0] pnl         [0:N_BOOKS-1],
    input  logic        [7:0]  reject_cnt  [0:N_BOOKS-1],
    input  logic        [7:0]  token_cnt   [0:N_BOOKS-1],
    input  logic               bid_valid   [0:N_BOOKS-1],
    input  logic               ask_valid   [0:N_BOOKS-1],
    input  logic        [63:0] order_id_cnt,

    // AXI-Stream output (clk domain → axis_async_fifo → enc_clk arbiter)
    output logic [7:0]  tx_tdata,
    output logic        tx_tvalid,
    input  logic        tx_tready,
    output logic        tx_tlast,
    output logic        tx_tuser,

    // Latency stats from latency_monitor (clk domain)
    input  logic [31:0] lat_min,
    input  logic [31:0] lat_max,
    input  logic [31:0] lat_last,
    input  logic [31:0] lat_count,

    // UDP sideband — constant, registered once at reset
    output logic [47:0] tx_dst_mac,
    output logic [31:0] tx_dst_ip,
    output logic [15:0] tx_src_port,
    output logic [15:0] tx_dst_port,
    output logic [15:0] tx_length
);

    localparam int PKT_BYTES = 80;
    localparam int TP_W     = $clog2(TELEM_PERIOD + 1);
    localparam int IDX_W    = $clog2(PKT_BYTES);

    // Constant sideband
    assign tx_dst_mac  = GATEWAY_MAC;
    assign tx_dst_ip   = OUCH_DST_IP;
    assign tx_src_port = PORT_OUCH;
    assign tx_dst_port = PORT_TELEM;
    assign tx_length   = 16'(PKT_BYTES);
    assign tx_tuser    = 1'b0;

    // ---- Period timer ------------------------------------------
    logic [TP_W-1:0]  period_cnt;
    logic             fire;

    assign fire = (period_cnt == TP_W'(TELEM_PERIOD - 1));

    always_ff @(posedge clk) begin
        if (rst)       period_cnt <= '0;
        else if (fire) period_cnt <= '0;
        else           period_cnt <= period_cnt + 1'b1;
    end

    // ---- Snapshot registers ------------------------------------
    logic signed [31:0] l_pos [0:N_BOOKS-1];
    logic signed [31:0] l_pnl [0:N_BOOKS-1];
    logic        [7:0]  l_rej [0:N_BOOKS-1];
    logic        [7:0]  l_tok [0:N_BOOKS-1];
    logic               l_bv  [0:N_BOOKS-1];
    logic               l_av  [0:N_BOOKS-1];
    logic        [63:0] l_oid;
    logic        [31:0] seq_num;
    logic        [31:0] l_lat_min;
    logic        [31:0] l_lat_max;
    logic        [31:0] l_lat_last;
    logic        [31:0] l_lat_count;

    // ---- State machine -----------------------------------------
    typedef enum logic { IDLE, SEND } state_t;
    state_t          state;
    logic [IDX_W-1:0] byte_idx;

    // Byte serializer (combinational)
    // Header: bytes 0-15
    // Slot N: bytes 16+N*12 .. 27+N*12  (12B each)
    //   +0..+3: pos[31:0]
    //   +4..+7: pnl[31:0]
    //   +8:     reject
    //   +9:     token
    //   +10:    flags  {6'b0, ask_valid, bid_valid}
    //   +11:    pad
    logic [7:0] cur_byte;
    always_comb begin
        case (byte_idx)
            // Magic + seq_num (bytes 0-7)
            7'd0:  cur_byte = 8'h48;
            7'd1:  cur_byte = 8'h46;
            7'd2:  cur_byte = 8'h54;
            7'd3:  cur_byte = 8'h01;
            7'd4:  cur_byte = seq_num[31:24];
            7'd5:  cur_byte = seq_num[23:16];
            7'd6:  cur_byte = seq_num[15:8];
            7'd7:  cur_byte = seq_num[7:0];
            // order_id_cnt (bytes 8-15)
            7'd8:  cur_byte = l_oid[63:56];
            7'd9:  cur_byte = l_oid[55:48];
            7'd10: cur_byte = l_oid[47:40];
            7'd11: cur_byte = l_oid[39:32];
            7'd12: cur_byte = l_oid[31:24];
            7'd13: cur_byte = l_oid[23:16];
            7'd14: cur_byte = l_oid[15:8];
            7'd15: cur_byte = l_oid[7:0];
            // Slot 0 (bytes 16-27)
            7'd16: cur_byte = l_pos[0][31:24];
            7'd17: cur_byte = l_pos[0][23:16];
            7'd18: cur_byte = l_pos[0][15:8];
            7'd19: cur_byte = l_pos[0][7:0];
            7'd20: cur_byte = l_pnl[0][31:24];
            7'd21: cur_byte = l_pnl[0][23:16];
            7'd22: cur_byte = l_pnl[0][15:8];
            7'd23: cur_byte = l_pnl[0][7:0];
            7'd24: cur_byte = l_rej[0];
            7'd25: cur_byte = l_tok[0];
            7'd26: cur_byte = {6'b0, l_av[0], l_bv[0]};
            7'd27: cur_byte = 8'h00;
            // Slot 1 (bytes 28-39)
            7'd28: cur_byte = l_pos[1][31:24];
            7'd29: cur_byte = l_pos[1][23:16];
            7'd30: cur_byte = l_pos[1][15:8];
            7'd31: cur_byte = l_pos[1][7:0];
            7'd32: cur_byte = l_pnl[1][31:24];
            7'd33: cur_byte = l_pnl[1][23:16];
            7'd34: cur_byte = l_pnl[1][15:8];
            7'd35: cur_byte = l_pnl[1][7:0];
            7'd36: cur_byte = l_rej[1];
            7'd37: cur_byte = l_tok[1];
            7'd38: cur_byte = {6'b0, l_av[1], l_bv[1]};
            7'd39: cur_byte = 8'h00;
            // Slot 2 (bytes 40-51)
            7'd40: cur_byte = l_pos[2][31:24];
            7'd41: cur_byte = l_pos[2][23:16];
            7'd42: cur_byte = l_pos[2][15:8];
            7'd43: cur_byte = l_pos[2][7:0];
            7'd44: cur_byte = l_pnl[2][31:24];
            7'd45: cur_byte = l_pnl[2][23:16];
            7'd46: cur_byte = l_pnl[2][15:8];
            7'd47: cur_byte = l_pnl[2][7:0];
            7'd48: cur_byte = l_rej[2];
            7'd49: cur_byte = l_tok[2];
            7'd50: cur_byte = {6'b0, l_av[2], l_bv[2]};
            7'd51: cur_byte = 8'h00;
            // Slot 3 (bytes 52-63)
            7'd52: cur_byte = l_pos[3][31:24];
            7'd53: cur_byte = l_pos[3][23:16];
            7'd54: cur_byte = l_pos[3][15:8];
            7'd55: cur_byte = l_pos[3][7:0];
            7'd56: cur_byte = l_pnl[3][31:24];
            7'd57: cur_byte = l_pnl[3][23:16];
            7'd58: cur_byte = l_pnl[3][15:8];
            7'd59: cur_byte = l_pnl[3][7:0];
            7'd60: cur_byte = l_rej[3];
            7'd61: cur_byte = l_tok[3];
            7'd62: cur_byte = {6'b0, l_av[3], l_bv[3]};
            7'd63: cur_byte = 8'h00;
            // Latency stats (bytes 64-79)
            7'd64: cur_byte = l_lat_min[31:24];
            7'd65: cur_byte = l_lat_min[23:16];
            7'd66: cur_byte = l_lat_min[15:8];
            7'd67: cur_byte = l_lat_min[7:0];
            7'd68: cur_byte = l_lat_max[31:24];
            7'd69: cur_byte = l_lat_max[23:16];
            7'd70: cur_byte = l_lat_max[15:8];
            7'd71: cur_byte = l_lat_max[7:0];
            7'd72: cur_byte = l_lat_last[31:24];
            7'd73: cur_byte = l_lat_last[23:16];
            7'd74: cur_byte = l_lat_last[15:8];
            7'd75: cur_byte = l_lat_last[7:0];
            7'd76: cur_byte = l_lat_count[31:24];
            7'd77: cur_byte = l_lat_count[23:16];
            7'd78: cur_byte = l_lat_count[15:8];
            7'd79: cur_byte = l_lat_count[7:0];
            default: cur_byte = 8'h00;
        endcase
    end

    assign tx_tdata  = cur_byte;
    assign tx_tvalid = (state == SEND);
    assign tx_tlast  = (state == SEND) && (byte_idx == IDX_W'(PKT_BYTES - 1));

    always_ff @(posedge clk) begin
        if (rst) begin
            state    <= IDLE;
            byte_idx <= '0;
            seq_num  <= '0;
            l_oid       <= '0;
            l_lat_min   <= '0;
            l_lat_max   <= '0;
            l_lat_last  <= '0;
            l_lat_count <= '0;
            for (int i = 0; i < N_BOOKS; i++) begin
                l_pos[i] <= '0;
                l_pnl[i] <= '0;
                l_rej[i] <= '0;
                l_tok[i] <= '0;
                l_bv[i]  <= 1'b0;
                l_av[i]  <= 1'b0;
            end
        end else begin
            case (state)
                IDLE: begin
                    if (fire) begin
                        // Latch snapshot and start streaming
                        for (int i = 0; i < N_BOOKS; i++) begin
                            l_pos[i] <= position[i];
                            l_pnl[i] <= pnl[i];
                            l_rej[i] <= reject_cnt[i];
                            l_tok[i] <= token_cnt[i];
                            l_bv[i]  <= bid_valid[i];
                            l_av[i]  <= ask_valid[i];
                        end
                        l_oid       <= order_id_cnt;
                        l_lat_min   <= lat_min;
                        l_lat_max   <= lat_max;
                        l_lat_last  <= lat_last;
                        l_lat_count <= lat_count;
                        seq_num  <= seq_num + 32'h1;
                        state    <= SEND;
                        byte_idx <= '0;
                    end
                end
                SEND: begin
                    if (tx_tready) begin
                        if (byte_idx == IDX_W'(PKT_BYTES - 1))
                            state <= IDLE;
                        else
                            byte_idx <= byte_idx + 1'b1;
                    end
                end
            endcase
        end
    end

endmodule
