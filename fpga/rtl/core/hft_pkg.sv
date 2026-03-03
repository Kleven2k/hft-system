// ============================================================
// hft_pkg.sv — Shared constants, types, and structs
// Import with: import hft_pkg::*;
// ============================================================

package hft_pkg;

    // ---- Network Identity ----------------------------------
    localparam bit [47:0] LOCAL_MAC   = 48'h02_00_00_00_00_01;
    localparam bit [31:0] LOCAL_IP    = 32'hC0_A8_01_0A;   // 192.168.1.10
    localparam bit [31:0] SUBNET_MASK = 32'hFF_FF_FF_00;
    localparam bit [31:0] GATEWAY_IP  = 32'hC0_A8_01_01;

    // ---- Clock ---------------------------------------------
    localparam int CLK_FREQ_HZ = 125_000_000;

    // ---- Protocol Ports ------------------------------------
    localparam bit [15:0] PORT_ITCH = 16'd17010;  // NASDAQ ITCH 5.0
    localparam bit [15:0] PORT_OUCH = 16'd42000;  // OUCH order entry
    localparam bit [15:0] PORT_MGMT = 16'd9000;   // GUI / host management

    // ---- Order Types ---------------------------------------
    typedef enum logic [1:0] {
        ORD_BUY    = 2'b00,
        ORD_SELL   = 2'b01
    } order_side_t;

    typedef enum logic [1:0] {
        ORD_MARKET = 2'b00,
        ORD_LIMIT  = 2'b01
    } order_type_t;

    // ---- Order struct (strategy → order engine) ------------
    typedef struct packed {
        logic [63:0]  order_id;
        logic [31:0]  price;       // fixed-point: price × 10000
        logic [31:0]  quantity;
        logic [15:0]  symbol_id;
        order_side_t  side;
        order_type_t  ord_type;
        logic         valid;
    } order_t;

    // ---- Quote struct (market data → strategy) -------------
    typedef struct packed {
        logic [63:0]  timestamp;   // nanoseconds from ITCH
        logic [31:0]  price;
        logic [31:0]  shares;
        logic [15:0]  symbol_id;
        logic         is_bid;
        logic         valid;
    } quote_t;

    // ---- Timing constants ----------------------------------
    localparam int PHY_RESET_CYCLES = CLK_FREQ_HZ / 100;     // 10ms
    localparam int UART_BAUD_DIV    = CLK_FREQ_HZ / 115_200;

endpackage