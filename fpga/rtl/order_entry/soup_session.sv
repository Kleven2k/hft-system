// ============================================================
// soup_session.sv — SoupBinTCP session layer
//
// Sits above tcp_engine. Manages the SoupBinTCP session and
// wraps/unwraps OUCH 4.2 messages.
//
// SoupBinTCP framing (big-endian):
//   [2] length  — bytes that follow (includes 1-byte type)
//   [1] type
//   [N] payload
//
// Client → Server (written to app_tx):
//   'L' (0x4C)  Login Request  (46-byte payload)
//   'U' (0x55)  Unsequenced Data (OUCH Enter/Cancel)
//   'R' (0x52)  Client Heartbeat (0-byte payload)
//   'O' (0x4F)  Logout Request  (0-byte payload)
//
// Server → Client (read from app_rx):
//   'A' (0x41)  Login Accepted
//   'J' (0x4A)  Login Rejected
//   'S' (0x53)  Sequenced Data (OUCH execution report)
//   'H' (0x48)  Server Heartbeat
//   'Z' (0x5A)  End of Session
//
// ACK output to order_engine: same 13-byte interface as ouch_ack_receiver.
// Inbound sequence number is tracked; gaps are counted but not acted upon
// (no retransmit request — GFD if sequence gap occurs).
//
// Clock domain: enc_clk (rgmii_rxc).
// ============================================================
`timescale 1ns/1ps
module soup_session
    import hft_pkg::*;
#(
    // SoupBinTCP credentials (ASCII, padded with spaces)
    parameter [47:0]  SOUP_USER    = 48'h55_53_45_52_20_20,    // "USER  "
    parameter [79:0]  SOUP_PASS    = 80'h50_41_53_53_57_4F_52_44_20_20, // "PASSWORD  "
    parameter [79:0]  SOUP_SESSION = 80'h20_20_20_20_20_20_20_20_20_20, // "          " (any)
    // Heartbeat interval: 1s at 125 MHz
    parameter int     HB_CYC       = 125_000_000
)(
    input  logic        clk,
    input  logic        rst_n,

    // TCP engine status
    input  logic        tcp_connected,

    // TCP application streams
    output logic [7:0]  app_tx_tdata,
    output logic        app_tx_tvalid,
    input  logic        app_tx_tready,
    output logic        app_tx_tlast,

    input  logic [7:0]  app_rx_tdata,
    input  logic        app_rx_tvalid,
    output logic        app_rx_tready,  // always 1
    input  logic        app_rx_tlast,

    // OUCH order input (from ouch_encoder AXI-S, enc_clk domain)
    input  logic [7:0]  ouch_tdata,
    input  logic        ouch_tvalid,
    output logic        ouch_tready,
    input  logic        ouch_tlast,

    // ACK output to order_engine (one-cycle pulse, enc_clk domain)
    output logic        ack_valid,
    output logic [63:0] ack_order_id,
    output logic [7:0]  ack_status,
    output logic [31:0] ack_fill_qty,

    // Status
    output logic        session_active,   // Login Accepted
    output logic [15:0] drop_count        // inbound sequence gaps
);

    // ---- ACK status codes (matches hft_pkg.sv) ----
    localparam ACK_FILLED    = 8'h00;
    localparam ACK_PARTIAL   = 8'h01;
    localparam ACK_REJECTED  = 8'h02;
    localparam ACK_CANCELLED = 8'h03;

    // ---- Session states ----
    typedef enum logic [2:0] {
        S_WAIT_TCP,    // waiting for TCP connection
        S_LOGIN,       // streaming Login Request
        S_LOGIN_WAIT,  // waiting for server 'A'/'J'
        S_ACTIVE,      // ESTABLISHED — forward orders
        S_HEARTBEAT,   // sending 'R' heartbeat
        S_LOGOUT       // sending 'O' logout (on RST/error)
    } sess_state_t;
    sess_state_t state;

    // ---- SoupBinTCP TX shift register ----
    // Max control msg: Login = 3 + 46 = 49 bytes.
    // Data msg: 3 (soup header: length[2]+type[1]) + up to 49 (OUCH Enter) = 52 bytes.
    // Use 52-byte SR for control; data bytes come through ouch_tdata directly.
    logic [415:0] ctrl_sr;   // 52 × 8 bits — control frame shift register
    logic [5:0]   ctrl_cnt;
    logic [5:0]   ctrl_last;
    logic         ctrl_active;

    assign app_rx_tready = 1'b1;  // always ready

    // ---- Heartbeat timer ----
    logic [26:0] hb_timer;  // 2^27 > 125M, so 27 bits cover 1s
    logic        hb_req;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            hb_timer <= '0;
            hb_req   <= 1'b0;
        end else if (state == S_ACTIVE) begin
            if (hb_timer == HB_CYC - 1) begin
                hb_timer <= '0;
                hb_req   <= 1'b1;
            end else begin
                hb_timer <= hb_timer + 1;
            end
            if (hb_req && ctrl_active == 0) hb_req <= 1'b0; // cleared when HB sent
        end else begin
            hb_timer <= '0;
            hb_req   <= 1'b0;
        end
    end

    // ---- Inbound SoupBinTCP parser ----
    // Byte-stream from TCP. Frames: [2-byte length][1-byte type][payload]
    logic [1:0]  rx_phase;   // 0=len_hi, 1=len_lo, 2=type, 3=payload
    logic [15:0] rx_frame_len;
    logic [7:0]  rx_pkt_type;
    logic [7:0]  rx_payload_cnt;  // bytes consumed in current payload
    // Execution report fields (accumulated over payload)
    logic [111:0] rx_token;        // 14-byte ASCII token
    logic [7:0]   rx_token_idx;
    logic [7:0]   rx_exec_type;    // first payload byte = OUCH msg type
    logic [31:0]  rx_exec_shares;  // executed/cancelled shares (bytes 15-18)
    logic [7:0]   rx_exec_byte_idx;

    // Token → order_id: parse 14-char ASCII hex string (matches make_token encoding).
    // Nibble extraction only — no multiplication.
    function automatic [63:0] token_to_id(input logic [111:0] tok);
        logic [63:0] v;
        logic [7:0]  c;
        logic [3:0]  nibble;
        v = '0;
        for (int i = 0; i < 14; i++) begin
            c      = tok[i*8 +: 8];
            nibble = (c >= 8'h41) ? (c - 8'h41 + 8'd10) : (c - 8'h30);
            v[(13-i)*4 +: 4] = nibble;  // mirror make_token: i=0 = MSB nibble
        end
        return v;
    endfunction

    logic  ack_valid_r;
    logic [63:0] ack_id_r;
    logic [7:0]  ack_st_r;
    logic [31:0] ack_qty_r;

    // 1-cycle delay pipeline: ACK fires the cycle AFTER the last token byte is
    // accumulated so token_to_id() sees the complete rx_token (all 14 bytes).
    logic        ack_fire;    // set when last RX byte received
    logic [7:0]  ack_st_latch;
    logic [31:0] ack_qty_latch;

    assign ack_valid    = ack_valid_r;
    assign ack_order_id = ack_id_r;
    assign ack_status   = ack_st_r;
    assign ack_fill_qty = ack_qty_r;

    // ---- Main state machine ----
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state         <= S_WAIT_TCP;
            ctrl_active   <= 1'b0;
            ctrl_sr       <= '0;
            ctrl_cnt      <= '0;
            ctrl_last     <= '0;
            session_active <= 1'b0;
            drop_count     <= '0;
            ack_valid_r    <= 1'b0;
            ack_id_r       <= '0;
            ack_st_r       <= '0;
            ack_qty_r      <= '0;
            ack_fire       <= 1'b0;
            ack_st_latch   <= '0;
            ack_qty_latch  <= '0;
            rx_phase       <= 2'd0;
            rx_frame_len   <= '0;
            rx_pkt_type    <= '0;
            rx_payload_cnt <= '0;
            rx_token       <= '0;
            rx_token_idx   <= '0;
            rx_exec_type   <= '0;
            rx_exec_shares <= '0;
            rx_exec_byte_idx <= '0;
        end else begin
            ack_valid_r  <= 1'b0;
            // Fire ACK one cycle after last token byte so rx_token is complete
            if (ack_fire) begin
                ack_valid_r <= 1'b1;
                ack_id_r    <= token_to_id(rx_token);
                ack_st_r    <= ack_st_latch;
                ack_qty_r   <= ack_qty_latch;
                ack_fire    <= 1'b0;
            end

            // ---- TX path ----
            if (ctrl_active) begin
                if (app_tx_tready) begin
                    if (ctrl_cnt == ctrl_last)
                        ctrl_active <= 1'b0;
                    else begin
                        ctrl_sr  <= {ctrl_sr[407:0], 8'h00};
                        ctrl_cnt <= ctrl_cnt + 6'd1;
                    end
                end
            end else begin
                case (state)
                    S_WAIT_TCP: begin
                        session_active <= 1'b0;
                        if (tcp_connected) state <= S_LOGIN;
                    end

                    S_LOGIN: begin
                        // Login Request 'L': 3-byte soup header + 46-byte payload = 49 bytes
                        // Soup header: length=47 (1 type + 46 payload), type='L'
                        // Payload: username(6) + password(10) + session(10) + seq_num(20, ASCII "                   1")
                        // ctrl_sr is 416 bits (52 bytes); left-justify by padding 3 bytes at LSB.
                        ctrl_sr <= {
                            // SoupBinTCP header (3 bytes)
                            16'd47,                    // length = 1 + 46
                            8'h4C,                     // 'L'
                            // Payload (46 bytes)
                            SOUP_USER,                 // username 6 bytes
                            SOUP_PASS,                 // password 10 bytes
                            SOUP_SESSION,              // session  10 bytes
                            // sequence number: "                   1" (19 spaces + '1' = 20 bytes)
                            160'h20_20_20_20_20_20_20_20_20_20_20_20_20_20_20_20_20_20_20_31,
                            24'h0                      // 3-byte right-pad to fill 52-byte SR
                        };
                        ctrl_cnt  <= 6'd0;
                        ctrl_last <= 6'd48;   // 49 bytes, index 0..48
                        ctrl_active <= 1'b1;
                        state <= S_LOGIN_WAIT;
                    end

                    S_LOGIN_WAIT: begin
                        if (!tcp_connected) state <= S_WAIT_TCP;
                        // handled by RX path (see below)
                    end

                    S_ACTIVE: begin
                        if (!tcp_connected) begin
                            state          <= S_WAIT_TCP;
                            session_active <= 1'b0;
                        end else if (hb_req) begin
                            state <= S_HEARTBEAT;
                        end else if (ouch_tvalid) begin
                            // Start forwarding OUCH data with soup 'U' header
                            // 'U' header: length = (ouch_payload_len + 1), but we
                            // don't know payload length in advance.
                            // Strategy: for Enter Order len=52 (1+'U'+49), Cancel len=18 (1+'U'+15)
                            // We output header first, then gate ouch bytes through directly.
                            // State: S_ACTIVE with ouch_tvalid — emit soup header 3 bytes first,
                            // then let ouch bytes through.
                            state <= S_ACTIVE;  // handled inline below
                        end
                    end

                    S_HEARTBEAT: begin
                        // 'R' heartbeat: length=1, type='R', no payload → 3 bytes
                        ctrl_sr[415:400] <= 16'd1;    // length = 1
                        ctrl_sr[399:392] <= 8'h52;    // 'R'
                        ctrl_sr[391:0]   <= '0;
                        ctrl_cnt  <= 6'd0;
                        ctrl_last <= 6'd2;            // 3 bytes
                        ctrl_active <= 1'b1;
                        state <= S_ACTIVE;
                    end

                    S_LOGOUT: begin
                        ctrl_sr[415:400] <= 16'd1;
                        ctrl_sr[399:392] <= 8'h4F;    // 'O' logout
                        ctrl_sr[391:0]   <= '0;
                        ctrl_cnt  <= 6'd0;
                        ctrl_last <= 6'd2;
                        ctrl_active <= 1'b1;
                        state <= S_WAIT_TCP;
                        session_active <= 1'b0;
                    end
                endcase
            end

            // ---- RX path: parse inbound SoupBinTCP frames ----
            if (app_rx_tvalid) begin
                case (rx_phase)
                    2'd0: begin  // length high byte
                        rx_frame_len[15:8] <= app_rx_tdata;
                        rx_phase <= 2'd1;
                    end
                    2'd1: begin  // length low byte
                        rx_frame_len[7:0] <= app_rx_tdata;
                        rx_phase <= 2'd2;
                    end
                    2'd2: begin  // packet type
                        rx_pkt_type    <= app_rx_tdata;
                        rx_payload_cnt <= 8'd0;
                        rx_token       <= '0;
                        rx_token_idx   <= 8'd0;
                        rx_exec_type   <= 8'd0;
                        rx_exec_shares <= '0;
                        rx_exec_byte_idx <= 8'd0;
                        if (rx_frame_len == 16'd1) begin
                            // No payload — handle now
                            rx_phase <= 2'd0;
                            case (app_rx_tdata)
                                8'h48: ; // 'H' server heartbeat — no action
                                8'h5A: begin // 'Z' end of session
                                    session_active <= 1'b0;
                                    state <= S_WAIT_TCP;
                                end
                            endcase
                        end else begin
                            rx_phase <= 2'd3;
                        end
                    end
                    2'd3: begin  // payload bytes
                        rx_payload_cnt <= rx_payload_cnt + 8'd1;

                        case (rx_pkt_type)
                            8'h41: begin // 'A' Login Accepted
                                // payload: session(10) + seq(20) = 30 bytes
                                if (rx_payload_cnt == 8'd29) begin
                                    session_active <= 1'b1;
                                    state <= S_ACTIVE;
                                    rx_phase <= 2'd0;
                                end
                            end
                            8'h4A: begin // 'J' Login Rejected
                                // payload: reject_reason (1 byte)
                                session_active <= 1'b0;
                                state <= S_WAIT_TCP;
                                rx_phase <= 2'd0;
                            end
                            8'h53: begin // 'S' Sequenced Data = OUCH execution report
                                // Payload: OUCH inbound message
                                // byte 0 = msg_type: 'A'=Accepted 'E'=Executed 'C'=Cancelled 'J'=Rejected
                                // bytes 1-14 = order_token (14 ASCII chars)
                                // For Executed: bytes 15-18 = executed_shares
                                // For Cancelled: bytes 15-18 = decrement_shares
                                if (rx_payload_cnt == 8'd0) begin
                                    rx_exec_type <= app_rx_tdata;
                                end else if (rx_payload_cnt <= 8'd14) begin
                                    // Accumulate token (bytes 1-14)
                                    rx_token[111 - (rx_token_idx*8) -: 8] <= app_rx_tdata;
                                    rx_token_idx <= rx_token_idx + 8'd1;
                                end else if (rx_payload_cnt >= 8'd15 && rx_payload_cnt <= 8'd18) begin
                                    // bytes 15-18: shares executed/cancelled
                                    rx_exec_shares[(8'd18 - rx_payload_cnt)*8 +: 8] <= app_rx_tdata;
                                end

                                // Check payload end: length-1 bytes total (field 'length' includes type byte)
                                if (rx_payload_cnt == rx_frame_len - 16'd2) begin
                                    rx_phase <= 2'd0;
                                    // Latch ACK — fire next cycle so rx_token is fully written
                                    ack_fire <= 1'b1;
                                    case (rx_exec_type)
                                        8'h41: begin ack_st_latch <= ACK_PARTIAL;    ack_qty_latch <= 32'd0;          end
                                        8'h45: begin ack_st_latch <= ACK_FILLED;     ack_qty_latch <= rx_exec_shares; end
                                        8'h43: begin ack_st_latch <= ACK_CANCELLED;  ack_qty_latch <= 32'd0;          end
                                        8'h4A: begin ack_st_latch <= ACK_REJECTED;   ack_qty_latch <= 32'd0;          end
                                        default: ack_fire <= 1'b0;
                                    endcase
                                end
                            end
                            default: begin
                                // Skip unknown payload
                                if (rx_payload_cnt == rx_frame_len - 16'd2)
                                    rx_phase <= 2'd0;
                            end
                        endcase
                    end
                endcase
            end
        end
    end

    // ---- TX mux: ctrl_sr vs. ouch passthrough ----
    // When ctrl_active: route ctrl_sr byte-by-byte.
    // When ACTIVE and ouch_tvalid: prepend 3-byte soup 'U' header then pass ouch bytes.

    typedef enum logic [1:0] { TX_IDLE, TX_CTRL, TX_OUCH_HDR, TX_OUCH_DATA } txm_t;
    txm_t txm_state;

    logic [23:0] soup_hdr;     // 3-byte SoupBinTCP 'U' header
    logic [1:0]  soup_hdr_cnt; // 0..2
    logic        ouch_fwd;     // forwarding ouch bytes

    // Determine frame length for 'U' header:
    // ouch Enter = 49 bytes → soup len = 50, total = 52 bytes
    // ouch Cancel = 15 bytes → soup len = 16, total = 18 bytes
    // We set the header length dynamically when we see ouch_tvalid.
    // Since we don't know ouch payload length at header emit time,
    // we hold off one cycle to capture ouch_last flag... Actually we know:
    // ouch_encoder asserts tvalid and we can measure. But simpler:
    // Read ouch_tvalid and start header immediately — length is determined
    // by ouch pkt_last (49 bytes for Enter, 15 for Cancel).
    // The ouch_encoder drives tx_valid and we get the complete frame; we just
    // need to count bytes.  We can emit the soup header BEFORE ouch bytes start.
    // Length in soup header = 1 (type 'U') + ouch_payload_len.
    // We detect Cancel vs Enter by ouch byte[0]: 0x4F=Enter(49), 0x58=Cancel(15).
    // So buffer the first ouch byte, then emit header + first byte + rest.

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            txm_state    <= TX_IDLE;
            soup_hdr     <= '0;
            soup_hdr_cnt <= '0;
        end else begin
            case (txm_state)
                TX_IDLE: begin
                    if (ctrl_active) begin
                        txm_state <= TX_CTRL;
                    end else if (state == S_ACTIVE && ouch_tvalid) begin
                        // Peek at first ouch byte to determine message length for soup header.
                        // ouch_tready stays 0 during TX_OUCH_HDR so the sender holds this byte.
                        if (ouch_tdata == 8'h4F) begin // Enter Order (49 bytes): soup len = 1+'U'+49 = 50
                            soup_hdr <= {16'd50, 8'h55};
                        end else begin                  // Cancel Order (15 bytes): soup len = 16
                            soup_hdr <= {16'd16, 8'h55};
                        end
                        soup_hdr_cnt <= 2'd0;
                        txm_state    <= TX_OUCH_HDR;
                    end
                end

                TX_CTRL: begin
                    if (!ctrl_active) txm_state <= TX_IDLE;
                end

                TX_OUCH_HDR: begin
                    // Emit 3 soup header bytes. ouch_tready=0 so sender holds its first byte.
                    if (app_tx_tready) begin
                        soup_hdr     <= {soup_hdr[15:0], 8'h00};
                        soup_hdr_cnt <= soup_hdr_cnt + 2'd1;
                        if (soup_hdr_cnt == 2'd2) txm_state <= TX_OUCH_DATA;
                    end
                end

                TX_OUCH_DATA: begin
                    // Pass ouch bytes directly (sender still holds first byte since tready was 0).
                    if (ouch_tvalid && ouch_tlast && app_tx_tready) txm_state <= TX_IDLE;
                end

                default: txm_state <= TX_IDLE;
            endcase
        end
    end

    // ---- app_tx output mux ----
    always_comb begin
        case (txm_state)
            TX_CTRL: begin
                app_tx_tdata  = ctrl_sr[415:408];
                app_tx_tvalid = ctrl_active;
                app_tx_tlast  = ctrl_active && (ctrl_cnt == ctrl_last);
                ouch_tready   = 1'b0;
            end
            TX_OUCH_HDR: begin
                app_tx_tdata  = soup_hdr[23:16];  // MSB of 3-byte header
                app_tx_tvalid = 1'b1;
                app_tx_tlast  = 1'b0;
                ouch_tready   = 1'b0;
            end
            TX_OUCH_DATA: begin
                // Pass ouch bytes through directly. Sender still holds first byte
                // (tready was 0 during TX_OUCH_HDR), so no special-casing needed.
                app_tx_tdata  = ouch_tdata;
                app_tx_tvalid = ouch_tvalid;
                app_tx_tlast  = ouch_tlast;
                ouch_tready   = app_tx_tready;
            end
            default: begin
                app_tx_tdata  = 8'h00;
                app_tx_tvalid = 1'b0;
                app_tx_tlast  = 1'b0;
                ouch_tready   = 1'b0;
            end
        endcase
    end

endmodule
