// ============================================================
// ouch_ack_receiver.sv — OUCH ACK inbound parser
//
// Passively monitors the shared UDP RX stream (tready=1 from
// market_data_parser) and decodes 13-byte ACK messages arriving
// on PORT_OUCH_ACK.
//
// ACK frame format (13 bytes, big-endian):
//   [7:0]   order_id  — full 64-bit order token
//   [8]     status    — 0x00=filled, 0x01=partial, 0x02=rejected,
//                       0x03=cancelled
//   [12:9]  fill_qty  — shares filled this message (0 for cancel/reject)
//
// The module is a passive sniffer: it does not drive tready.
// market_data_parser permanently holds tready=1, so every valid
// byte is visible here as rx_tvalid=1.
//
// Clock domain: rgmii_rxc (125 MHz at 1G)
// ============================================================
`timescale 1ns/1ps
module ouch_ack_receiver
    import hft_pkg::*;
(
    input  logic        clk,
    input  logic        rst,

    // Passive tap on shared UDP RX stream
    input  logic [7:0]  rx_tdata,
    input  logic        rx_tvalid,
    input  logic        rx_tlast,
    input  logic [15:0] rx_dst_port,

    // ACK output — one-cycle pulse
    output logic        ack_valid,
    output logic [63:0] ack_order_id,
    output logic [7:0]  ack_status,
    output logic [31:0] ack_fill_qty
);

    typedef enum logic [1:0] { IDLE, RECV, DRAIN } state_t;

    state_t      state;
    logic [3:0]  byte_cnt;
    logic [63:0] id_buf;
    logic [7:0]  status_buf;
    logic [31:0] qty_buf;

    always_ff @(posedge clk) begin
        if (rst) begin
            state        <= IDLE;
            byte_cnt     <= '0;
            id_buf       <= '0;
            status_buf   <= '0;
            qty_buf      <= '0;
            ack_valid    <= 1'b0;
            ack_order_id <= '0;
            ack_status   <= '0;
            ack_fill_qty <= '0;
        end else begin
            ack_valid <= 1'b0;

            case (state)

                IDLE: begin
                    if (rx_tvalid) begin
                        if (rx_dst_port == PORT_OUCH_ACK) begin
                            // Byte 0: MSB of order_id[63:56]
                            id_buf[63:56] <= rx_tdata;
                            byte_cnt      <= 4'd1;
                            state         <= rx_tlast ? IDLE : RECV;
                        end else begin
                            if (!rx_tlast) state <= DRAIN;
                        end
                    end
                end

                RECV: begin
                    if (rx_tvalid) begin
                        case (byte_cnt)
                            4'd1:  id_buf[55:48]  <= rx_tdata;
                            4'd2:  id_buf[47:40]  <= rx_tdata;
                            4'd3:  id_buf[39:32]  <= rx_tdata;
                            4'd4:  id_buf[31:24]  <= rx_tdata;
                            4'd5:  id_buf[23:16]  <= rx_tdata;
                            4'd6:  id_buf[15: 8]  <= rx_tdata;
                            4'd7:  id_buf[ 7: 0]  <= rx_tdata;
                            4'd8:  status_buf     <= rx_tdata;   // status byte
                            4'd9:  qty_buf[31:24] <= rx_tdata;
                            4'd10: qty_buf[23:16] <= rx_tdata;
                            4'd11: qty_buf[15: 8] <= rx_tdata;
                            4'd12: begin                          // final fill_qty byte
                                ack_order_id <= id_buf;
                                ack_status   <= status_buf;
                                ack_fill_qty <= {qty_buf[31:8], rx_tdata};
                                ack_valid    <= 1'b1;
                            end
                            default: ;
                        endcase
                        byte_cnt <= byte_cnt + 4'd1;
                        if (rx_tlast) state <= IDLE;
                        else if (byte_cnt == 4'd12) state <= DRAIN;
                    end
                end

                DRAIN: begin
                    if (rx_tvalid && rx_tlast) state <= IDLE;
                end

                default: state <= IDLE;

            endcase
        end
    end

endmodule
