// ============================================================
// ouch_encoder.sv — OUCH 4.2 message serializer
//
// Enter Order ('O', 49 bytes):
//   [0]      'O' (0x4F)
//   [1-14]   order_token  — 14-byte ASCII, order_id zero-padded left
//   [15]     buy_sell     — 'B' (0x42) or 'S' (0x53)
//   [16-19]  shares       — uint32 big-endian
//   [20-25]  stock        — 6-byte ASCII, right-space-padded (from STOCK_n param)
//   [26-29]  price        — uint32 big-endian ($0.0001 per unit)
//   [30-33]  time_in_force— uint32 (99998 = IOC)
//   [34-39]  firm         — 6-byte ASCII, right-space-padded (from FIRM param)
//   [40]     display      — 'Y'
//   [41]     capacity     — from CAPACITY param ('A' agency / 'P' principal)
//   [42]     iso_eligible — 'N'
//   [43-46]  min_qty      — uint32 (0 = no minimum)
//   [47]     cross_type   — 'N'
//   [48]     customer_type— ' '
//
// Cancel Order ('X', 15 bytes):
//   [0]      'X' (0x58)
//   [1-14]   order_token  — same format as above
//
// Output: bare OUCH bytes as AXI-Stream (no SoupBinTCP framing — added above).
// Clock domain: enc_clk (rgmii_rxc).
// ============================================================
`timescale 1ns/1ps
module ouch_encoder
    import hft_pkg::*;
#(
    // One 6-byte ASCII stock symbol per slot (right-space-padded).
    // E.g. "AAPL  " → 8'h41,8'h41,8'h50,8'h4C,8'h20,8'h20
    parameter [47:0] STOCK_0    = 48'h41_41_50_4C_20_20,  // "AAPL  "
    parameter [47:0] STOCK_1    = 48'h4D_53_46_54_20_20,  // "MSFT  "
    parameter [47:0] STOCK_2    = 48'h41_4D_5A_4E_20_20,  // "AMZN  "
    parameter [47:0] STOCK_3    = 48'h54_53_4C_41_20_20,  // "TSLA  "
    // 6-byte MPID / firm code
    parameter [47:0] FIRM       = 48'h4D_59_46_52_4D_20,  // "MYFIRM"
    // 1-byte capacity: 'A' agency (0x41) or 'P' principal (0x50)
    parameter [7:0]  CAPACITY   = 8'h41,                  // 'A'
    // Time-in-force: 99998 = IOC
    parameter [31:0] TIF        = 32'd99998
)(
    input  logic        clk,
    input  logic        rst,

    // Order input (one-cycle pulse)
    input  order_t      order_in,
    input  logic        order_valid,

    // OUCH bytes output (no SoupBinTCP header — added by soup_session)
    output logic [7:0]  tx_tdata,
    output logic        tx_tvalid,
    input  logic        tx_tready,
    output logic        tx_tlast
);

    // Stock symbol lookup by symbol_id[1:0]
    function automatic [47:0] stock_sym(input logic [1:0] sid);
        case (sid)
            2'd0: return STOCK_0;
            2'd1: return STOCK_1;
            2'd2: return STOCK_2;
            2'd3: return STOCK_3;
        endcase
    endfunction

    // Convert 64-bit order_id to 14-byte ASCII hex token (MSB first).
    // Hex avoids division/modulo — just nibble extraction + ASCII offset.
    // 14 hex chars cover 56 bits (up to 0xFFFFFFFFFFFFFF ≈ 72 quadrillion orders).
    // order_id[55:0] is used; upper 8 bits are ignored (counter never reaches 2^56).
    function automatic [111:0] make_token(input logic [63:0] oid);
        logic [111:0] t;
        logic [3:0]   nibble;
        for (int i = 0; i < 14; i++) begin
            nibble = oid[(13-i)*4 +: 4];
            t[i*8 +: 8] = (nibble < 4'd10) ? (8'h30 + {4'h0, nibble})
                                            : (8'h41 + {4'h0, nibble} - 8'd10);
        end
        return t;
    endfunction

    // ---- Shift register ----
    // Max frame: 49 bytes (Enter Order). Store as 392-bit SR.
    // MSB of SR is always the next output byte.
    logic [391:0] shift_reg;  // 49 × 8 bits
    logic [5:0]   byte_cnt;
    logic         tx_active;
    logic [5:0]   pkt_last;

    assign tx_tdata  = shift_reg[391:384];
    assign tx_tvalid = tx_active;
    assign tx_tlast  = tx_active && (byte_cnt == pkt_last);

    always_ff @(posedge clk) begin
        if (rst) begin
            tx_active <= 1'b0;
            shift_reg <= '0;
            byte_cnt  <= '0;
            pkt_last  <= '0;
        end else if (!tx_active) begin
            if (order_valid) begin
                if (order_in.cancel) begin
                    // ---- Cancel: 15 bytes ----
                    shift_reg[391:272] <= {
                        8'h58,                          // 'X'
                        make_token(order_in.order_id)   // [1-14] token
                    };
                    shift_reg[271:0]   <= '0;
                    pkt_last  <= 6'd14;
                end else begin
                    // ---- Enter Order: 49 bytes ----
                    shift_reg <= {
                        8'h4F,                                          // [0]  'O'
                        make_token(order_in.order_id),                  // [1-14] token
                        (order_in.side == ORD_BUY) ? 8'h42 : 8'h53,   // [15] B/S
                        order_in.quantity[31:24],                       // [16]
                        order_in.quantity[23:16],                       // [17]
                        order_in.quantity[15:8],                        // [18]
                        order_in.quantity[7:0],                         // [19]
                        stock_sym(order_in.symbol_id[1:0]),             // [20-25]
                        order_in.price[31:24],                          // [26]
                        order_in.price[23:16],                          // [27]
                        order_in.price[15:8],                           // [28]
                        order_in.price[7:0],                            // [29]
                        TIF[31:24],                                     // [30]
                        TIF[23:16],                                     // [31]
                        TIF[15:8],                                      // [32]
                        TIF[7:0],                                       // [33]
                        FIRM,                                           // [34-39]
                        8'h59,                                          // [40] 'Y' display
                        CAPACITY,                                       // [41] capacity
                        8'h4E,                                          // [42] 'N' ISO
                        32'd0,                                          // [43-46] min_qty=0
                        8'h4E,                                          // [47] 'N' cross_type
                        8'h20                                           // [48] ' ' customer
                    };
                    pkt_last  <= 6'd48;
                end
                byte_cnt  <= 6'd0;
                tx_active <= 1'b1;
            end
        end else begin
            if (tx_tready) begin
                if (byte_cnt == pkt_last) begin
                    tx_active <= 1'b0;
                end else begin
                    shift_reg <= {shift_reg[383:0], 8'h00};
                    byte_cnt  <= byte_cnt + 6'd1;
                end
            end
        end
    end

endmodule
