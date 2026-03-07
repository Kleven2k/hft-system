// ============================================================
// market_data_parser.sv
// Parses binary market data message from UDP payload.
//
// Message format (20 bytes total):
//   [0]       msg_type  — 'A'(0x41)=bid, 'B'(0x42)=ask
//   [1..8]    timestamp — 8 bytes, big-endian nanoseconds
//   [9..12]   price     — 4 bytes, big-endian fixed-point
//   [13..16]  shares    — 4 bytes, big-endian
//   [17..18]  symbol_id — 2 bytes, big-endian
//   [19]      reserved
// ============================================================
module market_data_parser
    import hft_pkg::*;
(
    input  logic        clk,
    input  logic        rst,
    axis_if.slave       udp_rx,
    output quote_t      quote_out,
    output logic        quote_valid   // one-cycle pulse
);

    typedef enum logic [1:0] {
        IDLE,
        READ_BODY,
        OUTPUT
    } state_t;

    state_t      state;
    logic [7:0]  msg_type;
    logic [7:0]  byte_count;
    logic [63:0] timestamp;
    logic [31:0] price;
    logic [31:0] shares;
    logic [15:0] symbol_id;
    logic        is_bid;

    assign udp_rx.tready = 1'b1;

    always_ff @(posedge clk) begin
        if (rst) begin
            state            <= IDLE;
            byte_count       <= 0;
            quote_valid      <= 0;
            quote_out.valid     <= 0;
            quote_out.timestamp <= 0;
            quote_out.price     <= 0;
            quote_out.shares    <= 0;
            quote_out.symbol_id <= 0;
            quote_out.is_bid    <= 0;
            timestamp        <= 0;
            price            <= 0;
            shares           <= 0;
            symbol_id        <= 0;
            is_bid           <= 0;
        end
        else begin
            quote_valid <= 0;

            case (state)

                IDLE: begin
                    if (udp_rx.tvalid) begin
                        msg_type   <= udp_rx.tdata;
                        is_bid     <= (udp_rx.tdata == 8'h41); // 'A'=bid, 'B'=ask
                        byte_count <= 0;
                        state      <= READ_BODY;
                    end
                end

                READ_BODY: begin
                    if (udp_rx.tvalid) begin
                        byte_count <= byte_count + 1;
                        case (byte_count)
                            // timestamp [1..8] — 8 bytes
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
                        endcase

                        if (byte_count == 17 || udp_rx.tlast)
                            state <= OUTPUT;
                    end
                end

                OUTPUT: begin
                    quote_out.timestamp <= timestamp;
                    quote_out.price     <= price;
                    quote_out.shares    <= shares;
                    quote_out.symbol_id <= symbol_id;
                    quote_out.is_bid    <= is_bid;
                    quote_out.valid     <= 1;
                    quote_valid         <= 1;
                    state               <= IDLE;
                end

                default: state <= IDLE;

            endcase
        end
    end

endmodule