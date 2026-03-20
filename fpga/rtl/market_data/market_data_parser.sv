// ============================================================
// market_data_parser.sv
// Parses binary market data message from UDP payload.
//
// Message format (20 bytes total):
//   [0]       msg_type  — see table below
//   [1..8]    timestamp — 8 bytes, big-endian nanoseconds
//   [9..12]   price     — 4 bytes, big-endian fixed-point
//   [13..16]  shares    — 4 bytes, big-endian
//   [17..18]  symbol_id — 2 bytes, big-endian
//   [19]      reserved
//
// msg_type encoding:
//   0x41 'A' — bid ADD
//   0x42 'B' — ask ADD
//   0x43 'C' — bid CANCEL
//   0x44 'D' — ask CANCEL
//   0x45 'E' — bid EXECUTE
//   0x46 'F' — ask EXECUTE
//
// Only packets with rx_dst_port == PORT_ITCH are parsed.
// All other ports (ACK, MGMT, etc.) are silently drained.
// Truncated packets (tlast before byte 18) are also discarded
// to prevent stale price/shares from a previous quote leaking
// into a new spurious output.
// ============================================================
module market_data_parser
    import hft_pkg::*;
(
    input  logic        clk,
    input  logic        rst,
    axis_if.slave       udp_rx,
    input  logic [15:0] rx_dst_port,  // sideband: must equal PORT_ITCH to parse
    output quote_t      quote_out,
    output logic        quote_valid   // one-cycle pulse
);

    typedef enum logic [1:0] {
        IDLE,
        READ_BODY,
        DRAIN,
        OUTPUT
    } state_t;

    state_t      state;
    logic [7:0]  byte_count;
    logic [63:0] timestamp;
    logic [31:0] price;
    logic [31:0] shares;
    logic [15:0] symbol_id;
    logic        is_bid;
    op_t         op;

    assign udp_rx.tready = 1'b1;

    always_ff @(posedge clk) begin
        if (rst) begin
            state               <= IDLE;
            byte_count          <= 0;
            quote_valid         <= 0;
            quote_out.valid     <= 0;
            quote_out.timestamp <= 0;
            quote_out.price     <= 0;
            quote_out.shares    <= 0;
            quote_out.symbol_id <= 0;
            quote_out.is_bid    <= 0;
            quote_out.op        <= OP_ADD;
            timestamp           <= 0;
            price               <= 0;
            shares              <= 0;
            symbol_id           <= 0;
            is_bid              <= 0;
            op                  <= OP_ADD;
        end else begin
            quote_valid <= 0;

            case (state)

                IDLE: begin
                    if (udp_rx.tvalid) begin
                        if (rx_dst_port == PORT_ITCH) begin
                            // Decode msg_type → is_bid + op
                            case (udp_rx.tdata)
                                8'h41: begin is_bid <= 1; op <= OP_ADD;     end // 'A' bid ADD
                                8'h42: begin is_bid <= 0; op <= OP_ADD;     end // 'B' ask ADD
                                8'h43: begin is_bid <= 1; op <= OP_CANCEL;  end // 'C' bid CANCEL
                                8'h44: begin is_bid <= 0; op <= OP_CANCEL;  end // 'D' ask CANCEL
                                8'h45: begin is_bid <= 1; op <= OP_EXECUTE; end // 'E' bid EXECUTE
                                8'h46: begin is_bid <= 0; op <= OP_EXECUTE; end // 'F' ask EXECUTE
                                default: begin is_bid <= 0; op <= OP_ADD;   end
                            endcase
                            byte_count <= 0;
                            state      <= udp_rx.tlast ? IDLE : READ_BODY;
                        end else begin
                            // Not an ITCH packet — drain until end of this packet
                            if (!udp_rx.tlast) state <= DRAIN;
                        end
                    end
                end

                READ_BODY: begin
                    if (udp_rx.tvalid) begin
                        case (byte_count)
                            // timestamp [1..8]
                            0: timestamp[63:56] <= udp_rx.tdata;
                            1: timestamp[55:48] <= udp_rx.tdata;
                            2: timestamp[47:40] <= udp_rx.tdata;
                            3: timestamp[39:32] <= udp_rx.tdata;
                            4: timestamp[31:24] <= udp_rx.tdata;
                            5: timestamp[23:16] <= udp_rx.tdata;
                            6: timestamp[15: 8] <= udp_rx.tdata;
                            7: timestamp[ 7: 0] <= udp_rx.tdata;
                            // price [9..12]
                            8:  price[31:24] <= udp_rx.tdata;
                            9:  price[23:16] <= udp_rx.tdata;
                            10: price[15: 8] <= udp_rx.tdata;
                            11: price[ 7: 0] <= udp_rx.tdata;
                            // shares [13..16]
                            12: shares[31:24] <= udp_rx.tdata;
                            13: shares[23:16] <= udp_rx.tdata;
                            14: shares[15: 8] <= udp_rx.tdata;
                            15: shares[ 7: 0] <= udp_rx.tdata;
                            // symbol_id [17..18]
                            16: symbol_id[15:8] <= udp_rx.tdata;
                            17: symbol_id[ 7:0] <= udp_rx.tdata;
                            default: ;
                        endcase
                        byte_count <= byte_count + 8'h1;

                        if (byte_count == 8'd17)
                            state <= OUTPUT;           // complete message
                        else if (udp_rx.tlast)
                            state <= IDLE;             // truncated: discard
                    end
                end

                DRAIN: begin
                    // Consume non-ITCH packet bytes until end-of-packet
                    if (udp_rx.tvalid && udp_rx.tlast) state <= IDLE;
                end

                OUTPUT: begin
                    quote_out.timestamp <= timestamp;
                    quote_out.price     <= price;
                    quote_out.shares    <= shares;
                    quote_out.symbol_id <= symbol_id;
                    quote_out.is_bid    <= is_bid;
                    quote_out.op        <= op;
                    quote_out.valid     <= 1;
                    quote_valid         <= 1;
                    state               <= IDLE;
                end

                default: state <= IDLE;

            endcase
        end
    end

endmodule
