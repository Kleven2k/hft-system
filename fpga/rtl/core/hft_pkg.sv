// ============================================================
// hft_pkg.sv — Shared constants, types, and structs
// Import with: import hft_pkg::*;
// ============================================================

package hft_pkg;

    // ---- Network Identity ----------------------------------
    // Change these to match your lab setup.
    // Use locally-administered MAC (bit 1 of byte 0 = 1).
    localparam bit [47:0] LOCAL_MAC   = 48'h02_00_00_00_00_01;
    localparam bit [31:0] LOCAL_IP    = 32'hC0_A8_01_0A;   // 192.168.1.10
    localparam bit [31:0] SUBNET_MASK = 32'hFF_FF_FF_00;   // /24
    localparam bit [31:0] GATEWAY_IP  = 32'hC0_A8_01_01;   // 192.168.1.1

    // ---- Clock ---------------------------------------------
    localparam int CLK_FREQ_HZ = 125_000_000;

    // ---- Protocol Port Numbers -----------------------------
    localparam bit [15:0] PORT_ITCH   = 16'd17010;  // NASDAQ ITCH 5.0 (market data)
    localparam bit [15:0] PORT_OUCH   = 16'd42000;  // OUCH order entry
    localparam bit [15:0] PORT_MGMT   = 16'd9000;   // management / GUI host interface

    // ---- AXI-Stream widths ---------------------------------
    localparam int AXIS_DATA_W = 8;    // byte-wide, matches verilog-ethernet MAC

    // ---- Order Types ---------------------------------------
    typedef enum logic [1:0] {
        ORD_BUY  = 2'b00,
        ORD_SELL = 2'b01
    } order_side_t;

    typedef enum logic [1:0] {
        ORD_MARKET = 2'b00,
        ORD_LIMIT  = 2'b01
    } order_type_t;

    // ---- Order struct (passed from strategy → order engine) 
    typedef struct packed {
        logic [63:0]  order_id;
        logic [31:0]  price;       // fixed-point: price * 10000
        logic [31:0]  quantity;
        logic [15:0]  symbol_id;   // internal symbol index
        order_side_t  side;
        order_type_t  ord_type;
        logic         valid;
    } order_t;

    // ---- Market Data (ITCH decoded, passed to strategy) ----
    typedef struct packed {
        logic [63:0]  timestamp;   // nanoseconds from ITCH message
        logic [31:0]  price;
        logic [31:0]  shares;
        logic [15:0]  symbol_id;
        logic         is_bid;      // 1 = bid, 0 = ask
        logic         valid;
    } quote_t;

    // ---- PHY reset hold time (10ms @ 125 MHz) --------------
    localparam int PHY_RESET_CYCLES = CLK_FREQ_HZ / 100;

    // ---- UART baud divisor (115200 @ 125 MHz) --------------
    localparam int UART_BAUD_DIV = CLK_FREQ_HZ / 115_200;

endpackage