// ============================================================
// market_data_parser.sv
// Parses binary market data from RAW MAC frames (not UDP payload).
//
// Strips ETH(14) + IP(20) + UDP(8) = 42-byte header, then parses
// the 20-byte payload directly. This avoids dependence on
// eth_stack_wrapper's tready behavior (which can lose bytes when
// mac_rx.tready=1 and udp_complete internally backpressures).
//
// Checks:
//   EtherType == 0x0800 (IPv4)   at frame bytes 12-13
//   IP proto  == 0x11  (UDP)     at frame byte 23
//   UDP dst   == PORT_ITCH       at frame bytes 36-37
//
// Payload format (bytes 42-61 of raw MAC frame):
//   [0]       msg_type  — 0x41=bid ADD, 0x42=ask ADD, etc.
//   [1..8]    timestamp — 8 bytes big-endian nanoseconds
//   [9..12]   price     — 4 bytes big-endian
//   [13..16]  shares    — 4 bytes big-endian
//   [17..18]  symbol_id — 2 bytes big-endian
//   [19]      reserved
// ============================================================
`timescale 1ns/1ps
module market_data_parser
    import hft_pkg::*;
(
    input  logic        clk,
    input  logic        rst,
    // Raw MAC RX (connected directly to mac_rx)
    input  logic [7:0]  rx_tdata,
    input  logic        rx_tvalid,
    output logic        rx_tready,
    input  logic        rx_tlast,
    input  logic        rx_tuser,
    // rx_dst_port unused (parsed internally)
    input  logic [15:0] rx_dst_port,
    output quote_t      quote_out,
    output logic        quote_valid   // one-cycle pulse
);

    typedef enum logic [1:0] {
        HEADER,     // bytes 0-41: ETH+IP+UDP headers
        TYPE,       // byte 42: msg_type
        BODY,       // bytes 43-60: parse + emit on byte 17
        DRAIN       // drain reserved byte + padding until tlast
    } state_t;

    state_t      state;
    logic [5:0]  hdr_cnt;   // 0-41 for header bytes
    logic [7:0]  byte_count; // 0-17 for body bytes

    // Header fields captured during HEADER state
    logic [7:0]  eth_type_h, eth_type_l;
    logic [7:0]  ip_proto;
    logic [7:0]  udp_dst_h, udp_dst_l;

    // Payload fields
    logic [63:0] timestamp;
    logic [31:0] price;
    logic [31:0] shares;
    logic [15:0] symbol_id;
    logic        is_bid;
    op_t         op;

    assign rx_tready = 1'b1;  // always ready

    // Frame validity: checked at byte 41 before transitioning to TYPE
    wire frame_ok = (eth_type_h == 8'h08) && (eth_type_l == 8'h00) &&
                    (ip_proto   == 8'h11) &&
                    ({udp_dst_h, udp_dst_l} == 16'(PORT_ITCH));

    always_ff @(posedge clk) begin
        if (rst) begin
            state       <= HEADER;
            hdr_cnt     <= '0;
            byte_count  <= '0;
            quote_valid <= 0;
            quote_out   <= '0;
            eth_type_h  <= '0; eth_type_l <= '0;
            ip_proto    <= '0;
            udp_dst_h   <= '0; udp_dst_l  <= '0;
            timestamp   <= '0;
            price       <= '0;
            shares      <= '0;
            symbol_id   <= '0;
            is_bid      <= '0;
            op          <= OP_ADD;
        end else begin
            quote_valid <= 0;

            case (state)

                HEADER: begin
                    if (rx_tvalid) begin
                        case (hdr_cnt)
                            6'd12: eth_type_h <= rx_tdata;
                            6'd13: eth_type_l <= rx_tdata;
                            6'd23: ip_proto   <= rx_tdata;
                            6'd36: udp_dst_h  <= rx_tdata;
                            6'd37: udp_dst_l  <= rx_tdata;
                            default: ;
                        endcase

                        if (rx_tlast) begin
                            hdr_cnt <= '0;          // short frame — reset
                        end else if (hdr_cnt == 6'd41) begin
                            hdr_cnt <= '0;
                            state   <= frame_ok ? TYPE : DRAIN;
                        end else begin
                            hdr_cnt <= hdr_cnt + 6'd1;
                        end
                    end
                end

                TYPE: begin
                    if (rx_tvalid) begin
                        case (rx_tdata)
                            8'h41: begin is_bid <= 1; op <= OP_ADD;     end
                            8'h42: begin is_bid <= 0; op <= OP_ADD;     end
                            8'h43: begin is_bid <= 1; op <= OP_CANCEL;  end
                            8'h44: begin is_bid <= 0; op <= OP_CANCEL;  end
                            8'h45: begin is_bid <= 1; op <= OP_EXECUTE; end
                            8'h46: begin is_bid <= 0; op <= OP_EXECUTE; end
                            default: begin is_bid <= 0; op <= OP_ADD;   end
                        endcase
                        byte_count <= '0;
                        state      <= rx_tlast ? HEADER : BODY;
                    end
                end

                BODY: begin
                    if (rx_tvalid) begin
                        case (byte_count)
                            // timestamp bytes 43-50 (body bytes 0-7)
                            8'd0: timestamp[63:56] <= rx_tdata;
                            8'd1: timestamp[55:48] <= rx_tdata;
                            8'd2: timestamp[47:40] <= rx_tdata;
                            8'd3: timestamp[39:32] <= rx_tdata;
                            8'd4: timestamp[31:24] <= rx_tdata;
                            8'd5: timestamp[23:16] <= rx_tdata;
                            8'd6: timestamp[15: 8] <= rx_tdata;
                            8'd7: timestamp[ 7: 0] <= rx_tdata;
                            // price bytes 51-54 (body bytes 8-11)
                            8'd8:  price[31:24] <= rx_tdata;
                            8'd9:  price[23:16] <= rx_tdata;
                            8'd10: price[15: 8] <= rx_tdata;
                            8'd11: price[ 7: 0] <= rx_tdata;
                            // shares bytes 55-58 (body bytes 12-15)
                            8'd12: shares[31:24] <= rx_tdata;
                            8'd13: shares[23:16] <= rx_tdata;
                            8'd14: shares[15: 8] <= rx_tdata;
                            8'd15: shares[ 7: 0] <= rx_tdata;
                            // symbol_id bytes 59-60 (body bytes 16-17)
                            8'd16: symbol_id[15:8] <= rx_tdata;
                            8'd17: symbol_id[ 7:0] <= rx_tdata;
                            default: ;
                        endcase
                        byte_count <= byte_count + 8'h1;

                                if (byte_count == 8'd17) begin
                            // Emit quote using rx_tdata directly for symbol_id[7:0]
                            // (register hasn't updated yet — use rx_tdata in-place)
                            quote_out.timestamp <= timestamp;
                            quote_out.price     <= price;
                            quote_out.shares    <= shares;
                            quote_out.symbol_id <= {symbol_id[15:8], rx_tdata};
                            quote_out.is_bid    <= is_bid;
                            quote_out.op        <= op;
                            quote_out.valid     <= 1;
                            quote_valid         <= 1;
                            state               <= DRAIN;  // consume reserved byte + padding
                        end else if (rx_tlast)
                            state <= HEADER;  // truncated: discard
                    end
                end

                DRAIN: begin
                    if (rx_tvalid && rx_tlast) state <= HEADER;
                end

            endcase
        end
    end

endmodule
