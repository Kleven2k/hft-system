// ============================================================
// uart_rx_config.sv — UART RX + price_base config + kill switch
//
// Receives frames from the PC. Two frame types:
//
// 5-byte price_base frame (big-endian):
//   Byte 0 : 0xA0 | slot[1:0]   sync + slot select (0–3)
//   Byte 1 : base[31:24]
//   Byte 2 : base[23:16]
//   Byte 3 : base[15:8]
//   Byte 4 : base[7:0]
//
// 1-byte kill-switch commands (Phase 16):
//   0xB0 — kill switch ON  (halt all new order emission)
//   0xB1 — kill switch OFF (resume order emission)
//
// Baud rate: 115200, 8N1
// Clock domain: clk (125 MHz)
// ============================================================
`timescale 1ns/1ps
module uart_rx_config
    import hft_pkg::*;
#(
    parameter int N_BOOKS     = 4,
    parameter int BAUD_DIV_TB = 0   // 0 = use hft_pkg::UART_BAUD_DIV; override in testbench
)(
    input  logic        clk,
    input  logic        rst,

    input  logic        uart_rx,   // async input from pin

    // Write port to price_base_table
    output logic                          wr_en,
    output logic [$clog2(N_BOOKS)-1:0]   wr_slot,
    output logic [31:0]                   wr_base,

    // Kill switch (Phase 16): latched by 0xB0/0xB1 UART commands
    output logic                          kill_switch
);

    localparam int BAUD_DIV  = (BAUD_DIV_TB > 0) ? BAUD_DIV_TB : UART_BAUD_DIV;
    localparam int HALF_DIV  = BAUD_DIV / 2;

    // ── 2-FF synchronizer ────────────────────────────────────
    logic rx_s1, rx_s2, rx_prev;

    always_ff @(posedge clk) begin
        rx_s1   <= uart_rx;
        rx_s2   <= rx_s1;
        rx_prev <= rx_s2;
    end

    // ── UART RX — 8N1 bit sampler ────────────────────────────
    typedef enum logic [1:0] { RX_IDLE, RX_START, RX_DATA, RX_STOP } rx_state_t;
    rx_state_t      rx_state;
    logic [15:0]    baud_cnt;
    logic [2:0]     bit_cnt;
    logic [7:0]     shift_reg;
    logic           rx_valid;   // one-cycle pulse per received byte
    logic [7:0]     rx_byte;

    always_ff @(posedge clk) begin
        if (rst) begin
            rx_state  <= RX_IDLE;
            baud_cnt  <= '0;
            bit_cnt   <= '0;
            shift_reg <= '0;
            rx_valid  <= 1'b0;
            rx_byte   <= '0;
        end else begin
            rx_valid <= 1'b0;

            case (rx_state)
                RX_IDLE: begin
                    // Falling edge = start bit
                    if (rx_prev && !rx_s2) begin
                        baud_cnt <= 16'(HALF_DIV);
                        rx_state <= RX_START;
                    end
                end

                RX_START: begin
                    if (baud_cnt != 0) begin
                        baud_cnt <= baud_cnt - 1'b1;
                    end else begin
                        if (!rx_s2) begin   // still low — valid start bit
                            baud_cnt <= 16'(BAUD_DIV);
                            bit_cnt  <= '0;
                            rx_state <= RX_DATA;
                        end else begin      // glitch, abort
                            rx_state <= RX_IDLE;
                        end
                    end
                end

                RX_DATA: begin
                    if (baud_cnt != 0) begin
                        baud_cnt <= baud_cnt - 1'b1;
                    end else begin
                        shift_reg <= {rx_s2, shift_reg[7:1]};  // LSB first
                        baud_cnt  <= 16'(BAUD_DIV);
                        if (bit_cnt == 3'd7)
                            rx_state <= RX_STOP;
                        else
                            bit_cnt <= bit_cnt + 1'b1;
                    end
                end

                RX_STOP: begin
                    if (baud_cnt != 0) begin
                        baud_cnt <= baud_cnt - 1'b1;
                    end else begin
                        if (rx_s2) begin    // stop bit high — valid frame
                            rx_byte  <= shift_reg;
                            rx_valid <= 1'b1;
                        end
                        rx_state <= RX_IDLE;
                    end
                end
            endcase
        end
    end

    // ── Frame parser ─────────────────────────────────────────
    // Frame: { 0xA0|slot, base[31:24], base[23:16], base[15:8], base[7:0] }
    typedef enum logic [2:0] { P_SYNC, P_B1, P_B2, P_B3, P_B4 } parser_state_t;
    parser_state_t   p_state;
    logic [1:0]      p_slot;
    logic [31:8]     p_base_hi;   // accumulates bytes 1–3

    always_ff @(posedge clk) begin
        if (rst) begin
            p_state    <= P_SYNC;
            p_slot     <= '0;
            p_base_hi  <= '0;
            wr_en      <= 1'b0;
            wr_slot    <= '0;
            wr_base    <= '0;
            kill_switch<= 1'b0;
        end else begin
            wr_en <= 1'b0;

            if (rx_valid) begin
                case (p_state)
                    P_SYNC: begin
                        if (rx_byte[7:2] == 6'b10_1000) begin
                            // 0xA0–0xA3: price_base 5-byte frame
                            p_slot  <= rx_byte[1:0];
                            p_state <= P_B1;
                        end else if (rx_byte == 8'hB0) begin
                            kill_switch <= 1'b1;   // kill ON
                        end else if (rx_byte == 8'hB1) begin
                            kill_switch <= 1'b0;   // kill OFF
                        end
                        // else: discard and keep waiting for sync
                    end

                    P_B1: begin
                        p_base_hi[31:24] <= rx_byte;
                        p_state          <= P_B2;
                    end

                    P_B2: begin
                        p_base_hi[23:16] <= rx_byte;
                        p_state          <= P_B3;
                    end

                    P_B3: begin
                        p_base_hi[15:8] <= rx_byte;
                        p_state         <= P_B4;
                    end

                    P_B4: begin
                        wr_en   <= 1'b1;
                        wr_slot <= p_slot;
                        wr_base <= {p_base_hi, rx_byte};
                        p_state <= P_SYNC;
                    end

                    default: p_state <= P_SYNC;
                endcase
            end
        end
    end

endmodule
