// ============================================================
// ouch_encoder.sv — OUCH order message AXI-Stream serializer
//
// New order  ('O', 20 bytes):
//   Byte  0   : msg_type = 0x4F ('O')
//   Bytes 1-2 : symbol_id [15:0]  big-endian
//   Byte  3   : side  0x42='B' / 0x53='S'
//   Bytes 4-7 : price [31:0]      big-endian
//   Bytes 8-11: quantity [31:0]   big-endian
//   Bytes 12-19: order_id [63:0]  big-endian
//
// Cancel order ('X', 9 bytes):
//   Byte  0   : msg_type = 0x58 ('X')
//   Bytes 1-8 : order_id [63:0]   big-endian
//
// UDP header timing constraint (eth_stack_wrapper.sv line 92):
//   udp_tx_hdr_valid = udp_tx_tvalid && udp_tx_tlast
//   → sideband signals (including tx_length) must be held stable
//     from first byte through tlast.
//
// Clock domain: rgmii_rxc
// ============================================================
`timescale 1ns/1ps
module ouch_encoder
    import hft_pkg::*;
#(
    parameter bit [31:0] DST_IP   = OUCH_DST_IP,
    parameter bit [47:0] DST_MAC  = GATEWAY_MAC,
    parameter bit [15:0] DST_PORT = PORT_OUCH
)(
    input  logic        clk,   // rgmii_rxc domain
    input  logic        rst,   // active-high synchronous

    // Order input (one-cycle pulse from order_cdc_bridge)
    input  order_t      order_in,
    input  logic        order_valid,

    // UDP TX AXI-Stream
    output logic [7:0]  tx_tdata,
    output logic        tx_tvalid,
    input  logic        tx_tready,
    output logic        tx_tlast,
    output logic        tx_tuser,

    // UDP sideband (held stable during entire packet)
    output logic [47:0] tx_dst_mac,
    output logic [31:0] tx_dst_ip,
    output logic [15:0] tx_src_port,
    output logic [15:0] tx_dst_port,
    output logic [15:0] tx_length
);

    // Fixed sideband — stable always
    assign tx_dst_mac  = DST_MAC;
    assign tx_dst_ip   = DST_IP;
    assign tx_src_port = PORT_OUCH;
    assign tx_dst_port = DST_PORT;
    assign tx_tuser    = 1'b0;

    // 20-byte shift register: MSB is the current output byte.
    // Cancel uses only bytes 0-8; new order uses all 20.
    logic [159:0] shift_reg;
    logic [4:0]   byte_cnt;
    logic         tx_active;
    logic [4:0]   pkt_last;   // index of final byte (8 or 19)

    assign tx_tdata  = shift_reg[159:152];
    assign tx_tvalid = tx_active;
    assign tx_tlast  = tx_active && (byte_cnt == pkt_last);
    assign tx_length = (pkt_last == 5'd8) ? 16'd9 : 16'd20;

    always_ff @(posedge clk) begin
        if (rst) begin
            tx_active <= 1'b0;
            shift_reg <= '0;
            byte_cnt  <= '0;
            pkt_last  <= '0;
        end else if (!tx_active) begin
            if (order_valid) begin
                if (order_in.cancel) begin
                    // ---- Cancel: 9 bytes ----
                    shift_reg <= {
                        8'h58,                       // byte 0: 'X'
                        order_in.order_id[63:56],    // byte 1
                        order_in.order_id[55:48],    // byte 2
                        order_in.order_id[47:40],    // byte 3
                        order_in.order_id[39:32],    // byte 4
                        order_in.order_id[31:24],    // byte 5
                        order_in.order_id[23:16],    // byte 6
                        order_in.order_id[15:8],     // byte 7
                        order_in.order_id[7:0],      // byte 8
                        88'h0                        // padding (not sent)
                    };
                    pkt_last  <= 5'd8;
                end else begin
                    // ---- New order: 20 bytes ----
                    shift_reg <= {
                        8'h4F,                                                    // byte 0
                        order_in.symbol_id[15:8],                                 // byte 1
                        order_in.symbol_id[7:0],                                  // byte 2
                        (order_in.side == ORD_BUY) ? 8'h42 : 8'h53,              // byte 3
                        order_in.price[31:24],                                    // byte 4
                        order_in.price[23:16],                                    // byte 5
                        order_in.price[15:8],                                     // byte 6
                        order_in.price[7:0],                                      // byte 7
                        order_in.quantity[31:24],                                 // byte 8
                        order_in.quantity[23:16],                                 // byte 9
                        order_in.quantity[15:8],                                  // byte 10
                        order_in.quantity[7:0],                                   // byte 11
                        order_in.order_id[63:56],                                 // byte 12
                        order_in.order_id[55:48],                                 // byte 13
                        order_in.order_id[47:40],                                 // byte 14
                        order_in.order_id[39:32],                                 // byte 15
                        order_in.order_id[31:24],                                 // byte 16
                        order_in.order_id[23:16],                                 // byte 17
                        order_in.order_id[15:8],                                  // byte 18
                        order_in.order_id[7:0]                                    // byte 19
                    };
                    pkt_last  <= 5'd19;
                end
                byte_cnt  <= 5'd0;
                tx_active <= 1'b1;
            end
        end else begin
            // Streaming — stall if downstream not ready
            if (tx_tready) begin
                if (byte_cnt == pkt_last) begin
                    tx_active <= 1'b0;
                end else begin
                    shift_reg <= {shift_reg[151:0], 8'h00};
                    byte_cnt  <= byte_cnt + 5'd1;
                end
            end
        end
    end

endmodule
