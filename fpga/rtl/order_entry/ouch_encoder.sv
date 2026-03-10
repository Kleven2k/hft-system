// ============================================================
// ouch_encoder.sv — OUCH order message AXI-Stream serializer
//
// Serializes an order_t to a 20-byte UDP payload:
//
//   Byte  0   : msg_type = 0x4F ('O')
//   Bytes 1-2 : symbol_id [15:0]  big-endian
//   Byte  3   : side  0x42='B' / 0x53='S'
//   Bytes 4-7 : price [31:0]      big-endian
//   Bytes 8-11: quantity [31:0]   big-endian
//   Bytes 12-19: order_id [63:0]  big-endian
//
// UDP header timing constraint (eth_stack_wrapper.sv line 92):
//   udp_tx_hdr_valid = udp_tx_tvalid && udp_tx_tlast
//   → sideband signals must be held stable from first byte
//     through tlast. This module never deasserts tvalid once
//     transmission starts (backpressure stalls but doesn't gap).
//
// Clock domain: rgmii_rxc
// ============================================================
`timescale 1ns/1ps
module ouch_encoder
    import hft_pkg::*;
#(
    parameter bit [31:0] DST_IP   = GATEWAY_IP,
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
    assign tx_length   = 16'd20;
    assign tx_tuser    = 1'b0;

    // 20-byte shift register: MSB is the current output byte
    logic [159:0] shift_reg;
    logic [4:0]   byte_cnt;   // 0–19
    logic         tx_active;

    assign tx_tdata  = shift_reg[159:152];
    assign tx_tvalid = tx_active;
    assign tx_tlast  = tx_active && (byte_cnt == 5'd19);

    always_ff @(posedge clk) begin
        if (rst) begin
            tx_active <= 1'b0;
            shift_reg <= '0;
            byte_cnt  <= '0;
        end else if (!tx_active) begin
            if (order_valid) begin
                // Build 20-byte message (MSB first = byte 0 in bits [159:152])
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
                byte_cnt  <= 5'd0;
                tx_active <= 1'b1;
            end
        end else begin
            // Streaming — stall if downstream not ready
            if (tx_tready) begin
                if (byte_cnt == 5'd19) begin
                    tx_active <= 1'b0;
                end else begin
                    shift_reg <= {shift_reg[151:0], 8'h00};
                    byte_cnt  <= byte_cnt + 5'd1;
                end
            end
        end
    end

endmodule
