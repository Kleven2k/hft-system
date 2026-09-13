// ============================================================
// tcp_engine.sv — Minimal single-connection TCP client
//
// Phase 27 rewrite: timing-safe architecture.
//
// Key design decisions:
//   • No always_comb — checksums computed in always_ff only.
//   • Checksum pipeline split across TWO cycles (S_TX_FOLD +
//     S_TX_PREP) to keep each cycle under 8 ns on Artix-7 -1.
//   • Header bytes streamed via a function (54-way mux on
//     registered values — synthesises as a balanced mux tree).
//   • 54-byte header buffer eliminated: bytes generated inline
//     during TX_HDR using hdr_byte(cnt).
//   • 96-byte payload buffer (pbuf) with running checksum
//     accumulator updated one word/cycle during TX_BUF.
//   • Single always_ff block — no blocking-assignment tasks.
//
// TX pipeline:
//   S_TX_BUF   : accept payload, accumulate pay_chk_acc
//   S_TX_FOLD  : 1 cycle — fold pay_chk_acc, pre-sum TCP terms
//   S_TX_PREP  : 1 cycle — compute ip_chk_reg, tcp_chk_reg
//   S_TX_HDR   : 54 cycles — stream ETH+IP+TCP header bytes
//   S_TX_DATA  : N cycles  — stream pbuf[0..pbuf_len-1]
//
// RX pipeline (parallel, byte-by-byte):
//   Parse ETH/ARP/IP/TCP headers, fire one-cycle event pulses.
//
// Frame types (tx_frame register selects hdr_byte() behaviour):
//   FT_SYN  — TCP SYN (no payload, seq=INIT_SEQ, ack=0)
//   FT_ACK  — TCP ACK (no payload, pure handshake/keepalive)
//   FT_DATA — TCP PSH+ACK with payload from pbuf
//   FT_FIN  — TCP FIN+ACK (no payload)
//
// Clock domain: enc_clk (rgmii_rxc, 125 MHz).
// ============================================================
`timescale 1ns/1ps
module tcp_engine
    import hft_pkg::*;
#(
    parameter [47:0] LOCAL_MAC   = 48'h02_00_00_00_00_01,
    parameter [31:0] LOCAL_IP    = 32'hC0_A8_01_0A,
    parameter [31:0] GATEWAY_IP  = 32'hC0_A8_01_01,
    parameter [31:0] SERVER_IP   = 32'hC0_A8_01_0B,
    parameter [15:0] LOCAL_PORT  = 16'd4201,
    parameter [15:0] SERVER_PORT = PORT_OUCH_TCP,
    parameter [31:0] INIT_SEQ    = 32'hA5C3_7B21,
    parameter [31:0] ARP_TIMEOUT = 32'd125_000_000,   // 1s
    parameter [31:0] SYN_TIMEOUT = 32'd125_000_000,   // 1s
    parameter [31:0] RECONNECT   = 32'd250_000_000    // 2s
)(
    input  logic        clk,
    input  logic        rst_n,

    // MAC RX (all frames — always ready)
    input  logic [7:0]  mac_rx_tdata,
    input  logic        mac_rx_tvalid,
    output logic        mac_rx_tready,
    input  logic        mac_rx_tlast,
    input  logic        mac_rx_tuser,

    // MAC TX (our frames only)
    output logic [7:0]  mac_tx_tdata,
    output logic        mac_tx_tvalid,
    input  logic        mac_tx_tready,
    output logic        mac_tx_tlast,
    output logic        mac_tx_tuser,

    // Application TX (from soup_session)
    input  logic [7:0]  app_tx_tdata,
    input  logic        app_tx_tvalid,
    output logic        app_tx_tready,
    input  logic        app_tx_tlast,

    // Application RX (to soup_session)
    output logic [7:0]  app_rx_tdata,
    output logic        app_rx_tvalid,
    input  logic        app_rx_tready,
    output logic        app_rx_tlast,

    output logic        connected,
    output logic        arp_done    // latches high when first ARP reply received
);

    assign mac_rx_tready = 1'b1;
    assign mac_tx_tuser  = 1'b0;

    // =========================================================
    // Pre-computed partial checksum constants (parameter math,
    // evaluated at elaboration — zero runtime logic)
    // =========================================================

    // IP checksum partial: sum of all constant IP header 16-bit words
    // (excluding total_len[16:17], identification[18:19], checksum[24:25])
    // = ver/IHL/TOS  + flags/frag + TTL/proto + src_ip_hi + src_ip_lo
    //   + dst_ip_hi  + dst_ip_lo
    localparam [31:0] IP_PARTIAL =
        32'h4500          // [14:15] version=4, IHL=5, TOS=0
      + 32'h4000          // [20:21] flags=DF, frag=0
      + 32'h4006          // [22:23] TTL=64, proto=TCP
      + LOCAL_IP[31:16]
      + LOCAL_IP[15:0]
      + SERVER_IP[31:16]
      + SERVER_IP[15:0];

    // TCP checksum partial: sum of pseudo-header constants + constant TCP fields
    // (excluding tcp_seg_len, seq, ack, flags, checksum[50:51])
    // pseudo : src_ip_hi + src_ip_lo + dst_ip_hi + dst_ip_lo + 0x0006
    // tcp    : src_port + dst_port + data_offset(0x5000) + window(0xFFFF) + urgent(0)
    localparam [31:0] TCP_PARTIAL =
        LOCAL_IP[31:16]
      + LOCAL_IP[15:0]
      + SERVER_IP[31:16]
      + SERVER_IP[15:0]
      + 32'h0006          // pseudo-header protocol field
      + LOCAL_PORT
      + SERVER_PORT
      + 32'h5000          // data_offset=5 (high nibble), reserved=0
      + 32'hFFFF;         // window = 65535

    // =========================================================
    // Types
    // =========================================================
    typedef enum logic [3:0] {
        S_INIT,
        S_ARP_SEND,
        S_ARP_WAIT,
        S_SYN_SEND,
        S_SYN_WAIT,
        S_HS_ACK,       // send ACK to complete 3-way handshake
        S_ESTABLISHED,
        S_TX_BUF,       // buffer app_tx payload + accumulate chksum
        S_TX_FOLD,      // 1 cycle: fold pay_chk_acc, pre-sum TCP terms
        S_TX_PREP,      // 1 cycle: compute ip_chk_reg, tcp_chk_reg
        S_TX_CHK,       // 1 cycle: fold ta_partial → tcp_chk_reg (no-payload frames)
        S_TX_HDR,       // 54 cycles: stream ETH+IP+TCP header
        S_TX_DATA,      // N cycles: stream pbuf
        S_TX_ACK_ONLY,  // 54 cycles: stream pure-ACK header
        S_FIN_SEND,
        S_CLOSED
    } state_t;

    typedef enum logic [1:0] { FT_SYN, FT_ACK, FT_DATA, FT_FIN } frame_t;

    // =========================================================
    // Registers
    // =========================================================
    state_t      state;
    logic        ack_pending;       // set when ev_rx_data fires outside S_ESTABLISHED
    frame_t      tx_frame;          // what frame we're building
    logic [47:0] gw_mac;
    logic [31:0] snd_seq;
    logic [31:0] rcv_nxt;
    logic [15:0] ip_id;
    logic [15:0] ip_total_len;      // current frame's IP total length
    logic [15:0] tcp_seg_len;       // current frame's TCP segment length
    logic [7:0]  tx_flags;          // current frame's TCP flags
    logic [31:0] tx_seq;            // current frame's SEQ number

    // Checksum registers (written in S_TX_PREP, read during S_TX_HDR)
    logic [15:0] ip_chk_reg;
    logic [15:0] tcp_chk_reg;

    // Payload buffer
    logic [7:0]  pbuf [0:95];
    logic [6:0]  pbuf_wr;           // write pointer during TX_BUF
    logic [6:0]  pbuf_len;          // total payload bytes
    logic [6:0]  pbuf_rd;           // read pointer during TX_DATA

    // Payload checksum accumulator (ones-complement running sum)
    logic [31:0] pay_chk_acc;
    logic [7:0]  pay_chk_hi;        // MSB of current 16-bit word (odd byte holder)

    // Pipeline registers written in S_TX_FOLD, read in S_TX_PREP
    logic [15:0] pay_chk_folded;    // folded 16-bit payload checksum
    logic [31:0] ta_partial;        // pre-summed TCP pseudo-hdr + seq/ack terms

    // TX header counter
    logic [5:0]  hdr_cnt;

    // Timer for retries
    logic [31:0] timer;

    // Next state to go to after TX completes
    state_t      tx_next;

    // RX parser
    logic [7:0]  rx_pos;
    logic [15:0] rx_eth_type;
    logic [7:0]  rx_ip_proto;
    logic [31:0] rx_ip_src, rx_ip_dst;
    logic [15:0] rx_ip_total_len;
    logic [15:0] rx_tcp_src_port, rx_tcp_dst_port;
    logic [31:0] rx_tcp_seq, rx_tcp_ack_num;
    logic [7:0]  rx_tcp_flags;
    logic [7:0]  rx_tcp_data_off;   // raw data-offset byte (high nibble × 4 = hdr bytes)
    logic [7:0]  rx_payload_start;  // byte offset where TCP payload begins
    logic        rx_relevant;
    logic [47:0] rx_arp_sender_mac;
    logic [31:0] rx_arp_sender_ip;
    logic        rx_arp_op2;        // seen OPER=2 in ARP

    // RX events (one-cycle pulses)
    logic        ev_arp_reply;
    logic        ev_syn_ack;
    logic        ev_rst;
    logic        ev_fin;
    logic        ev_rx_data;        // received TCP payload
    logic [15:0] ev_rx_payload_len; // payload bytes in that frame

    // Latched high when ev_arp_reply first fires (diagnostic LED)
    logic arp_done_r;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) arp_done_r <= 1'b0;
        else if (ev_arp_reply) arp_done_r <= 1'b1;
    assign arp_done = arp_done_r;

    assign connected = (state == S_ESTABLISHED) ||
                       (state == S_TX_BUF)       ||
                       (state == S_TX_FOLD)       ||
                       (state == S_TX_PREP)       ||
                       (state == S_TX_CHK)        ||
                       (state == S_TX_HDR)        ||
                       (state == S_TX_DATA)       ||
                       (state == S_TX_ACK_ONLY);

    // =========================================================
    // Header byte function
    // All inputs are REGISTERED — synthesises as a 54-way
    // balanced mux tree with no deep combinatorial paths.
    // =========================================================
    function automatic [7:0] hdr_byte(input [5:0] pos);
        case (pos)
            // ---- Ethernet header [0:13] ----
            6'd0:  return gw_mac[47:40];
            6'd1:  return gw_mac[39:32];
            6'd2:  return gw_mac[31:24];
            6'd3:  return gw_mac[23:16];
            6'd4:  return gw_mac[15:8];
            6'd5:  return gw_mac[7:0];
            6'd6:  return LOCAL_MAC[47:40];
            6'd7:  return LOCAL_MAC[39:32];
            6'd8:  return LOCAL_MAC[31:24];
            6'd9:  return LOCAL_MAC[23:16];
            6'd10: return LOCAL_MAC[15:8];
            6'd11: return LOCAL_MAC[7:0];
            6'd12: return 8'h08;
            6'd13: return 8'h00;
            // ---- IPv4 header [14:33] ----
            6'd14: return 8'h45;                  // ver=4, IHL=5
            6'd15: return 8'h00;                  // DSCP/ECN
            6'd16: return ip_total_len[15:8];
            6'd17: return ip_total_len[7:0];
            6'd18: return ip_id[15:8];
            6'd19: return ip_id[7:0];
            6'd20: return 8'h40;                  // flags: DF
            6'd21: return 8'h00;
            6'd22: return 8'h40;                  // TTL = 64
            6'd23: return 8'h06;                  // protocol = TCP
            6'd24: return ip_chk_reg[15:8];
            6'd25: return ip_chk_reg[7:0];
            6'd26: return LOCAL_IP[31:24];
            6'd27: return LOCAL_IP[23:16];
            6'd28: return LOCAL_IP[15:8];
            6'd29: return LOCAL_IP[7:0];
            6'd30: return SERVER_IP[31:24];
            6'd31: return SERVER_IP[23:16];
            6'd32: return SERVER_IP[15:8];
            6'd33: return SERVER_IP[7:0];
            // ---- TCP header [34:53] ----
            6'd34: return LOCAL_PORT[15:8];
            6'd35: return LOCAL_PORT[7:0];
            6'd36: return SERVER_PORT[15:8];
            6'd37: return SERVER_PORT[7:0];
            6'd38: return tx_seq[31:24];
            6'd39: return tx_seq[23:16];
            6'd40: return tx_seq[15:8];
            6'd41: return tx_seq[7:0];
            6'd42: return rcv_nxt[31:24];
            6'd43: return rcv_nxt[23:16];
            6'd44: return rcv_nxt[15:8];
            6'd45: return rcv_nxt[7:0];
            6'd46: return 8'h50;                  // data offset = 5
            6'd47: return tx_flags;
            6'd48: return 8'hFF;                  // window hi
            6'd49: return 8'hFF;                  // window lo
            6'd50: return tcp_chk_reg[15:8];
            6'd51: return tcp_chk_reg[7:0];
            6'd52: return 8'h00;                  // urgent
            6'd53: return 8'h00;
            default: return 8'h00;
        endcase
    endfunction

    // =========================================================
    // ARP request frame (60 bytes, nearly static)
    // =========================================================
    function automatic [7:0] arp_byte(input [5:0] pos);
        case (pos)
            6'd0:  return 8'hFF; 6'd1: return 8'hFF; 6'd2: return 8'hFF;
            6'd3:  return 8'hFF; 6'd4: return 8'hFF; 6'd5: return 8'hFF;
            6'd6:  return LOCAL_MAC[47:40]; 6'd7:  return LOCAL_MAC[39:32];
            6'd8:  return LOCAL_MAC[31:24]; 6'd9:  return LOCAL_MAC[23:16];
            6'd10: return LOCAL_MAC[15:8];  6'd11: return LOCAL_MAC[7:0];
            6'd12: return 8'h08; 6'd13: return 8'h06; // ARP ethertype
            6'd14: return 8'h00; 6'd15: return 8'h01; // HTYPE = Ethernet
            6'd16: return 8'h08; 6'd17: return 8'h00; // PTYPE = IPv4
            6'd18: return 8'h06; // HLEN
            6'd19: return 8'h04; // PLEN
            6'd20: return 8'h00; 6'd21: return 8'h01; // OPER = request
            6'd22: return LOCAL_MAC[47:40]; 6'd23: return LOCAL_MAC[39:32];
            6'd24: return LOCAL_MAC[31:24]; 6'd25: return LOCAL_MAC[23:16];
            6'd26: return LOCAL_MAC[15:8];  6'd27: return LOCAL_MAC[7:0];
            6'd28: return LOCAL_IP[31:24]; 6'd29: return LOCAL_IP[23:16];
            6'd30: return LOCAL_IP[15:8];  6'd31: return LOCAL_IP[7:0];
            6'd32: return 8'h00; 6'd33: return 8'h00; // target MAC (unknown)
            6'd34: return 8'h00; 6'd35: return 8'h00;
            6'd36: return 8'h00; 6'd37: return 8'h00;
            6'd38: return SERVER_IP[31:24];  6'd39: return SERVER_IP[23:16];
            6'd40: return SERVER_IP[15:8];   6'd41: return SERVER_IP[7:0];
            default: return 8'h00; // padding to 60 bytes
        endcase
    endfunction

    // =========================================================
    // Main always_ff
    // =========================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state         <= S_INIT;
            ack_pending   <= 1'b0;
            gw_mac        <= '0;
            snd_seq       <= INIT_SEQ;
            rcv_nxt       <= 32'd0;
            ip_id         <= 16'd1;
            ip_total_len  <= 16'd0;
            tcp_seg_len   <= 16'd0;
            tx_flags      <= 8'h00;
            tx_seq        <= INIT_SEQ;
            tx_frame      <= FT_SYN;
            tx_next       <= S_ESTABLISHED;
            ip_chk_reg    <= 16'd0;
            tcp_chk_reg   <= 16'd0;
            pbuf_wr       <= 7'd0;
            pbuf_len      <= 7'd0;
            pbuf_rd       <= 7'd0;
            pay_chk_acc    <= 32'd0;
            pay_chk_hi     <= 8'h00;
            pay_chk_folded <= 16'd0;
            ta_partial     <= 32'd0;
            hdr_cnt        <= 6'd0;
            timer         <= 32'd0;
            mac_tx_tvalid <= 1'b0;
            mac_tx_tdata  <= 8'h00;
            mac_tx_tlast  <= 1'b0;
            app_tx_tready <= 1'b0;
            app_rx_tvalid <= 1'b0;
            app_rx_tdata  <= 8'h00;
            app_rx_tlast  <= 1'b0;
            rx_pos        <= 8'd0;
            rx_eth_type   <= 16'd0;
            rx_ip_proto   <= 8'd0;
            rx_ip_src     <= 32'd0;
            rx_ip_dst     <= 32'd0;
            rx_ip_total_len <= 16'd0;
            rx_tcp_src_port <= 16'd0;
            rx_tcp_dst_port <= 16'd0;
            rx_tcp_seq    <= 32'd0;
            rx_tcp_ack_num <= 32'd0;
            rx_tcp_flags  <= 8'd0;
            rx_tcp_data_off <= 8'd0;
            rx_payload_start <= 8'd0;
            rx_relevant   <= 1'b0;
            rx_arp_sender_mac <= '0;
            rx_arp_sender_ip  <= '0;
            rx_arp_op2    <= 1'b0;
            ev_arp_reply  <= 1'b0;
            ev_syn_ack    <= 1'b0;
            ev_rst        <= 1'b0;
            ev_fin        <= 1'b0;
            ev_rx_data    <= 1'b0;
            ev_rx_payload_len <= 16'd0;
        end else begin
            // ---- clear one-cycle events ----
            ev_arp_reply <= 1'b0;
            ev_syn_ack   <= 1'b0;
            ev_rst       <= 1'b0;
            ev_fin       <= 1'b0;
            ev_rx_data   <= 1'b0;
            app_rx_tvalid <= 1'b0;

            // ---- MAC TX defaults (overridden below when streaming) ----
            // This ensures tvalid/tlast go low on the cycle AFTER the last
            // byte of each frame, rather than being forced low in the same
            // clock as tlast=1 (which would break AXI-S: consumer never sees
            // the last byte with tlast asserted).
            mac_tx_tvalid <= 1'b0;
            mac_tx_tlast  <= 1'b0;

            // =========================================================
            // RX PARSER (always running)
            // =========================================================
            if (mac_rx_tvalid) begin
                case (rx_pos)
                    8'd12: rx_eth_type[15:8] <= mac_rx_tdata;
                    8'd13: rx_eth_type[7:0]  <= mac_rx_tdata;
                    // ARP fields (non-overlapping)
                    8'd20: rx_arp_op2 <= 1'b0;
                    8'd21: rx_arp_op2 <= (rx_eth_type == 16'h0806) && (mac_rx_tdata == 8'h02);
                    8'd22: rx_arp_sender_mac[47:40] <= mac_rx_tdata;
                    8'd24: rx_arp_sender_mac[31:24] <= mac_rx_tdata;
                    8'd25: rx_arp_sender_mac[23:16] <= mac_rx_tdata;
                    // IPv4 non-overlapping fields
                    8'd16: rx_ip_total_len[15:8]    <= mac_rx_tdata;
                    8'd17: rx_ip_total_len[7:0]     <= mac_rx_tdata;
                    8'd32: rx_ip_dst[15:8]          <= mac_rx_tdata;
                    8'd33: rx_ip_dst[7:0]           <= mac_rx_tdata;
                    // Bytes 23, 26-31 overlap between ARP and IP — assign both.
                    // Each frame type only checks its own register, so this is safe.
                    8'd23: begin
                        rx_arp_sender_mac[39:32] <= mac_rx_tdata;  // ARP: sender MAC [39:32]
                        rx_ip_proto              <= mac_rx_tdata;  // IP:  protocol
                    end
                    8'd26: begin
                        rx_arp_sender_mac[15:8]  <= mac_rx_tdata;  // ARP: sender MAC [15:8]
                        rx_ip_src[31:24]         <= mac_rx_tdata;  // IP:  src [31:24]
                    end
                    8'd27: begin
                        rx_arp_sender_mac[7:0]   <= mac_rx_tdata;  // ARP: sender MAC [7:0]
                        rx_ip_src[23:16]         <= mac_rx_tdata;  // IP:  src [23:16]
                    end
                    8'd28: begin
                        rx_arp_sender_ip[31:24]  <= mac_rx_tdata;  // ARP: sender IP [31:24]
                        rx_ip_src[15:8]          <= mac_rx_tdata;  // IP:  src [15:8]
                    end
                    8'd29: begin
                        rx_arp_sender_ip[23:16]  <= mac_rx_tdata;  // ARP: sender IP [23:16]
                        rx_ip_src[7:0]           <= mac_rx_tdata;  // IP:  src [7:0]
                    end
                    8'd30: begin
                        rx_arp_sender_ip[15:8]   <= mac_rx_tdata;  // ARP: sender IP [15:8]
                        rx_ip_dst[31:24]         <= mac_rx_tdata;  // IP:  dst [31:24]
                    end
                    8'd31: begin
                        rx_arp_sender_ip[7:0]    <= mac_rx_tdata;  // ARP: sender IP [7:0]
                        rx_ip_dst[23:16]         <= mac_rx_tdata;  // IP:  dst [23:16]
                    end
                    // TCP fields (assuming IHL=5, TCP starts at byte 34)
                    8'd34: rx_tcp_src_port[15:8]    <= mac_rx_tdata;
                    8'd35: rx_tcp_src_port[7:0]     <= mac_rx_tdata;
                    8'd36: rx_tcp_dst_port[15:8]    <= mac_rx_tdata;
                    8'd37: rx_tcp_dst_port[7:0]     <= mac_rx_tdata;
                    8'd38: rx_tcp_seq[31:24]        <= mac_rx_tdata;
                    8'd39: rx_tcp_seq[23:16]        <= mac_rx_tdata;
                    8'd40: rx_tcp_seq[15:8]         <= mac_rx_tdata;
                    8'd41: rx_tcp_seq[7:0]          <= mac_rx_tdata;
                    8'd42: rx_tcp_ack_num[31:24]    <= mac_rx_tdata;
                    8'd43: rx_tcp_ack_num[23:16]    <= mac_rx_tdata;
                    8'd44: rx_tcp_ack_num[15:8]     <= mac_rx_tdata;
                    8'd45: rx_tcp_ack_num[7:0]      <= mac_rx_tdata;
                    8'd46: rx_tcp_data_off          <= mac_rx_tdata;
                    8'd47: begin
                        rx_tcp_flags <= mac_rx_tdata;
                        rx_payload_start <= 8'd34 + {rx_tcp_data_off[7:4], 2'b00};
                        rx_relevant <= (rx_eth_type == 16'h0800) &&
                                       (rx_ip_proto == 8'h06) &&
                                       (rx_ip_src   == SERVER_IP) &&
                                       (rx_ip_dst   == LOCAL_IP)  &&
                                       (rx_tcp_src_port == SERVER_PORT) &&
                                       (rx_tcp_dst_port == LOCAL_PORT);
                    end
                    default: ;
                endcase

                // Deliver TCP payload bytes to app_rx
                if (rx_relevant &&
                    rx_pos >= rx_payload_start &&
                    rx_pos < (8'd14 + rx_ip_total_len[7:0])) begin
                    app_rx_tdata  <= mac_rx_tdata;
                    app_rx_tvalid <= 1'b1;
                    app_rx_tlast  <= mac_rx_tlast;
                end

                if (mac_rx_tlast) begin
                    // Fire RX events
                    if (rx_eth_type == 16'h0806 && rx_arp_op2 &&
                        rx_arp_sender_ip == SERVER_IP)
                        ev_arp_reply <= 1'b1;

                    if (rx_relevant) begin
                        if (rx_tcp_flags[1] && rx_tcp_flags[4]) ev_syn_ack <= 1'b1;
                        if (rx_tcp_flags[2])                    ev_rst     <= 1'b1;
                        if (rx_tcp_flags[0])                    ev_fin     <= 1'b1;
                        if (rx_tcp_flags[3] || rx_tcp_flags[4]) begin
                            ev_rx_data        <= 1'b1;
                            ev_rx_payload_len <= rx_ip_total_len - 16'd40;
                            rcv_nxt     <= rx_tcp_seq + (rx_ip_total_len - 16'd40);
                            ack_pending <= 1'b1;
                        end
                    end
                    rx_pos      <= 8'd0;
                    rx_relevant <= 1'b0;
                end else begin
                    rx_pos <= rx_pos + 8'd1;
                end
            end  // mac_rx_tvalid

            // =========================================================
            // MAC TX STREAMING (active during S_TX_HDR/S_TX_DATA/S_ARP_SEND)
            // =========================================================
            if (state == S_TX_HDR || state == S_TX_ACK_ONLY) begin
                mac_tx_tdata  <= hdr_byte(hdr_cnt);
                mac_tx_tvalid <= 1'b1;
                mac_tx_tlast  <= (hdr_cnt == 6'd53) && (tx_frame != FT_DATA);
                if (mac_tx_tready) begin
                    if (hdr_cnt == 6'd53) begin
                        hdr_cnt <= 6'd0;
                        if (tx_frame == FT_DATA)
                            state <= S_TX_DATA;
                        else
                            state <= tx_next;
                    end else begin
                        hdr_cnt <= hdr_cnt + 6'd1;
                    end
                end
            end

            if (state == S_ARP_SEND) begin
                mac_tx_tdata  <= arp_byte(hdr_cnt);
                mac_tx_tvalid <= 1'b1;
                mac_tx_tlast  <= (hdr_cnt == 6'd59);
                if (mac_tx_tready) begin
                    if (hdr_cnt == 6'd59) begin
                        hdr_cnt <= 6'd0;
                        ip_id   <= ip_id + 16'd1;
                        state   <= S_ARP_WAIT;
                    end else begin
                        hdr_cnt <= hdr_cnt + 6'd1;
                    end
                end
            end

            if (state == S_TX_DATA) begin
                mac_tx_tdata  <= pbuf[pbuf_rd];
                mac_tx_tvalid <= 1'b1;
                mac_tx_tlast  <= (pbuf_rd == pbuf_len - 7'd1);
                if (mac_tx_tready) begin
                    if (pbuf_rd == pbuf_len - 7'd1) begin
                        pbuf_rd <= 7'd0;
                        ip_id   <= ip_id + 16'd1;
                        snd_seq <= snd_seq + {25'd0, pbuf_len};
                        state   <= tx_next;
                    end else begin
                        pbuf_rd <= pbuf_rd + 7'd1;
                    end
                end
            end

            // =========================================================
            // APP TX PAYLOAD BUFFERING
            // =========================================================
            app_tx_tready <= (state == S_TX_BUF);

            if (state == S_TX_BUF && app_tx_tvalid && app_tx_tready) begin
                pbuf[pbuf_wr] <= app_tx_tdata;
                pbuf_wr <= pbuf_wr + 7'd1;

                // Update running ones-complement checksum, one 16-bit word/cycle
                if (pbuf_wr[0] == 1'b1) begin
                    // Odd byte position: complete the word (MSB stored last cycle)
                    pay_chk_acc <= pay_chk_acc + {pay_chk_hi, app_tx_tdata};
                end else begin
                    pay_chk_hi <= app_tx_tdata;  // store MSB for next cycle
                end

                if (app_tx_tlast) begin
                    pbuf_len <= pbuf_wr + 7'd1;
                    // Handle odd-length payload (pad last byte with 0x00)
                    if (pbuf_wr[0] == 1'b0)
                        pay_chk_acc <= pay_chk_acc + {app_tx_tdata, 8'h00};
                    state <= S_TX_FOLD;
                end
            end

            // =========================================================
            // TX FOLD: fold pay_chk_acc + pre-sum TCP terms (1 cycle)
            // Breaks the 17-CARRY4 checksum path across two cycles.
            // =========================================================
            if (state == S_TX_FOLD) begin
                // Fold 32-bit accumulator → 16-bit ones-complement sum
                begin
                    logic [16:0] f1;
                    f1             = pay_chk_acc[15:0] + pay_chk_acc[31:16];
                    pay_chk_folded <= f1[15:0] + {15'd0, f1[16]};
                end

                // Pre-sum constant TCP pseudo-header + seq/ack terms
                ta_partial <= TCP_PARTIAL
                    + {16'd0, 16'd20 + {9'd0, pbuf_len}}  // tcp_seg_len in pseudo-hdr
                    + {16'd0, tx_seq[31:16]}
                    + {16'd0, tx_seq[15:0]}
                    + {16'd0, rcv_nxt[31:16]}
                    + {16'd0, rcv_nxt[15:0]};

                // Pre-compute length fields for hdr_byte (ready for S_TX_HDR)
                ip_total_len <= 16'd40 + {9'd0, pbuf_len};
                tcp_seg_len  <= 16'd20 + {9'd0, pbuf_len};

                state <= S_TX_PREP;
            end

            // =========================================================
            // TX PREP: compute ip_chk_reg and tcp_chk_reg (1 cycle)
            // ip_total_len, ta_partial, pay_chk_folded all registered.
            // Max combinatorial depth: 3 adds + fold (~7 CARRY4).
            // =========================================================
            if (state == S_TX_PREP) begin
                // ---- IP checksum (ip_total_len registered from S_TX_FOLD) ----
                begin
                    logic [31:0] ia;
                    ia = IP_PARTIAL
                       + {16'd0, ip_total_len}   // pre-computed in S_TX_FOLD
                       + {16'd0, ip_id};
                    ia = ia[15:0] + ia[31:16];
                    ip_chk_reg <= ~(ia[15:0] + {15'd0, ia[16]});
                end

                // ---- TCP checksum (ta_partial + pay_chk_folded from S_TX_FOLD) ----
                begin
                    logic [31:0] ta;
                    ta = ta_partial                          // pre-summed in S_TX_FOLD
                       + {16'd0, {8'h00, tx_flags}}         // flags byte
                       + {16'd0, pay_chk_folded};           // pre-folded payload checksum
                    ta = ta[15:0] + ta[31:16];
                    tcp_chk_reg <= ~(ta[15:0] + {15'd0, ta[16]});
                end

                state <= S_TX_HDR;
            end

            // =========================================================
            // TX CHK: fold ta_partial → tcp_chk_reg (1 cycle)
            // Used by no-payload frames (HS_ACK, ACK_ONLY, FIN).
            // ta_partial is the unfolded 32-bit ones-complement sum.
            // =========================================================
            if (state == S_TX_CHK) begin
                begin
                    logic [31:0] ta;
                    ta = ta_partial;
                    ta = ta[15:0] + ta[31:16];
                    tcp_chk_reg <= ~(ta[15:0] + {15'd0, ta[16]});
                end
                state <= S_TX_HDR;
            end

            // =========================================================
            // MAIN STATE MACHINE (non-TX states)
            // =========================================================
            case (state)
                S_INIT: begin
                    snd_seq     <= INIT_SEQ;
                    rcv_nxt     <= 32'd0;
                    ip_id       <= 16'd1;
                    pbuf_wr     <= 7'd0;
                    pbuf_len    <= 7'd0;
                    pbuf_rd     <= 7'd0;
                    pay_chk_acc <= 32'd0;
                    timer       <= 32'd0;
                    hdr_cnt     <= 6'd0;
                    state       <= S_ARP_SEND;
                end

                S_ARP_WAIT: begin
                    timer <= timer + 32'd1;
                    if (ev_arp_reply) begin
                        gw_mac <= rx_arp_sender_mac;
                        timer  <= 32'd0;
                        state  <= S_SYN_SEND;
                    end else if (timer >= ARP_TIMEOUT) begin
                        timer <= 32'd0;
                        state <= S_ARP_SEND;
                    end
                end

                S_SYN_SEND: begin
                    // Set up SYN frame parameters
                    ip_total_len <= 16'd40;  // IP(20) + TCP(20), no payload
                    tcp_seg_len  <= 16'd20;
                    tx_flags     <= 8'h02;   // SYN
                    tx_seq       <= INIT_SEQ;
                    tx_frame     <= FT_SYN;
                    tx_next      <= S_SYN_WAIT;
                    hdr_cnt      <= 6'd0;

                    // Compute SYN checksums inline (no payload, seq=INIT_SEQ, ack=0)
                    begin
                        logic [31:0] ia;
                        ia = IP_PARTIAL + 32'd40 + {16'd0, ip_id};
                        ia = ia[15:0] + ia[31:16];
                        ip_chk_reg <= ~(ia[15:0] + {15'd0, ia[16]});
                    end
                    begin
                        logic [31:0] ta;
                        ta = TCP_PARTIAL + 32'd20   // seg_len=20 (no payload)
                           + {16'd0, {8'h00, 8'h02}}// flags=SYN
                           + {16'd0, INIT_SEQ[31:16]}
                           + {16'd0, INIT_SEQ[15:0]}
                           + 32'd0                  // ack=0 for SYN
                           + 32'd0;                 // no payload
                        ta = ta[15:0] + ta[31:16];
                        tcp_chk_reg <= ~(ta[15:0] + {15'd0, ta[16]});
                    end
                    state <= S_TX_HDR;
                end

                S_SYN_WAIT: begin
                    timer <= timer + 32'd1;
                    if (ev_syn_ack) begin
                        rcv_nxt <= rx_tcp_seq + 32'd1;
                        snd_seq <= INIT_SEQ + 32'd1;
                        timer   <= 32'd0;
                        state   <= S_HS_ACK;
                    end else if (ev_rst) begin
                        timer <= 32'd0;
                        state <= S_CLOSED;
                    end else if (timer >= SYN_TIMEOUT) begin
                        timer <= 32'd0;
                        state <= S_SYN_SEND;
                    end
                end

                S_HS_ACK: begin
                    // Handshake ACK: seq=ISN+1, ack=rcv_nxt, no payload
                    ip_total_len <= 16'd40;
                    tcp_seg_len  <= 16'd20;
                    tx_flags     <= 8'h10;   // ACK
                    tx_seq       <= snd_seq;
                    tx_frame     <= FT_ACK;
                    tx_next      <= S_ESTABLISHED;
                    hdr_cnt      <= 6'd0;

                    begin
                        logic [31:0] ia;
                        ia = IP_PARTIAL + 32'd40 + {16'd0, ip_id};
                        ia = ia[15:0] + ia[31:16];
                        ip_chk_reg <= ~(ia[15:0] + {15'd0, ia[16]});
                    end
                    // Store unfolded sum; fold happens in S_TX_CHK (timing closure)
                    ta_partial <= TCP_PARTIAL + 32'd20
                        + {16'd0, {8'h00, 8'h10}}   // ACK flag
                        + {16'd0, snd_seq[31:16]}
                        + {16'd0, snd_seq[15:0]}
                        + {16'd0, rcv_nxt[31:16]}
                        + {16'd0, rcv_nxt[15:0]};
                    state <= S_TX_CHK;
                end

                S_ESTABLISHED: begin
                    if (ev_rst || ev_fin) begin
                        state <= S_FIN_SEND;
                    end else if (ev_rx_data && ev_rx_payload_len > 16'd0) begin
                        // rcv_nxt already updated in RX parser at tlast; also clear pending
                        ack_pending  <= 1'b0;
                        ip_total_len <= 16'd40;
                        tcp_seg_len  <= 16'd20;
                        tx_flags     <= 8'h10;
                        tx_seq       <= snd_seq;
                        tx_frame     <= FT_ACK;
                        tx_next      <= S_ESTABLISHED;
                        hdr_cnt      <= 6'd0;
                        begin
                            logic [31:0] ia;
                            ia = IP_PARTIAL + 32'd40 + {16'd0, ip_id};
                            ia = ia[15:0] + ia[31:16];
                            ip_chk_reg <= ~(ia[15:0] + {15'd0, ia[16]});
                        end
                        begin
                            logic [31:0] ta;
                            ta = TCP_PARTIAL + 32'd20
                                + {16'd0, {8'h00, 8'h10}}
                                + {16'd0, snd_seq[31:16]}
                                + {16'd0, snd_seq[15:0]}
                                + {16'd0, rcv_nxt[31:16]}
                                + {16'd0, rcv_nxt[15:0]};
                            ta_partial <= ta;
                        end
                        state <= S_TX_CHK;
                    end else if (ack_pending) begin
                        // ev_rx_data fired while we were busy; send ACK now
                        ack_pending  <= 1'b0;
                        ip_total_len <= 16'd40;
                        tcp_seg_len  <= 16'd20;
                        tx_flags     <= 8'h10;
                        tx_seq       <= snd_seq;
                        tx_frame     <= FT_ACK;
                        tx_next      <= S_ESTABLISHED;
                        hdr_cnt      <= 6'd0;
                        begin
                            logic [31:0] ia;
                            ia = IP_PARTIAL + 32'd40 + {16'd0, ip_id};
                            ia = ia[15:0] + ia[31:16];
                            ip_chk_reg <= ~(ia[15:0] + {15'd0, ia[16]});
                        end
                        begin
                            logic [31:0] ta;
                            ta = TCP_PARTIAL + 32'd20
                                + {16'd0, {8'h00, 8'h10}}
                                + {16'd0, snd_seq[31:16]}
                                + {16'd0, snd_seq[15:0]}
                                + {16'd0, rcv_nxt[31:16]}
                                + {16'd0, rcv_nxt[15:0]};
                            ta_partial <= ta;
                        end
                        state <= S_TX_CHK;
                    end else if (app_tx_tvalid) begin
                        // New data to send — go buffer it
                        pbuf_wr     <= 7'd0;
                        pbuf_len    <= 7'd0;
                        pay_chk_acc <= 32'd0;
                        tx_flags    <= 8'h18;   // PSH+ACK
                        tx_seq      <= snd_seq;
                        tx_frame    <= FT_DATA;
                        tx_next     <= S_ESTABLISHED;
                        state       <= S_TX_BUF;
                    end
                end

                S_FIN_SEND: begin
                    ip_total_len <= 16'd40;
                    tcp_seg_len  <= 16'd20;
                    tx_flags     <= 8'h11;   // FIN+ACK
                    tx_seq       <= snd_seq;
                    tx_frame     <= FT_FIN;
                    tx_next      <= S_CLOSED;
                    hdr_cnt      <= 6'd0;

                    begin
                        logic [31:0] ia;
                        ia = IP_PARTIAL + 32'd40 + {16'd0, ip_id};
                        ia = ia[15:0] + ia[31:16];
                        ip_chk_reg <= ~(ia[15:0] + {15'd0, ia[16]});
                    end
                    // Unfolded sum; fold in S_TX_CHK (timing closure)
                    ta_partial <= TCP_PARTIAL + 32'd20
                        + {16'd0, {8'h00, 8'h11}}
                        + {16'd0, snd_seq[31:16]}
                        + {16'd0, snd_seq[15:0]}
                        + {16'd0, rcv_nxt[31:16]}
                        + {16'd0, rcv_nxt[15:0]};
                    state <= S_TX_CHK;
                end

                S_CLOSED: begin
                    timer <= timer + 32'd1;
                    if (timer >= RECONNECT) begin
                        timer <= 32'd0;
                        state <= S_INIT;
                    end
                end

                // Streaming states — driven by parallel if-blocks above.
                // Must be listed here to prevent 'default' from overriding state.
                S_ARP_SEND, S_TX_BUF, S_TX_FOLD, S_TX_PREP, S_TX_CHK,
                S_TX_HDR,   S_TX_DATA, S_TX_ACK_ONLY: ;

                default: state <= S_INIT;
            endcase

        end  // rst_n
    end  // always_ff

endmodule
