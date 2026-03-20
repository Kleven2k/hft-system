// ============================================================
// hft_pkg.sv — Shared constants, types, and structs
// Import with: import hft_pkg::*;
// ============================================================
package hft_pkg;
    // ---- Network Identity ----------------------------------
    localparam bit [47:0] LOCAL_MAC   = 48'h02_00_00_00_00_01;
    localparam bit [31:0] LOCAL_IP    = 32'hC0_A8_01_0A;   // 192.168.1.10
    localparam bit [31:0] SUBNET_MASK = 32'hFF_FF_FF_00;   // /24
    localparam bit [31:0] GATEWAY_IP  = 32'hC0_A8_01_01;   // 192.168.1.1

    // ---- Clock ---------------------------------------------
    localparam int CLK_FREQ_HZ = 125_000_000;

    // ---- Protocol Port Numbers -----------------------------
    localparam bit [15:0] PORT_ITCH     = 16'd17010;
    localparam bit [15:0] PORT_OUCH     = 16'd42000;
    localparam bit [15:0] PORT_OUCH_ACK = 16'd42001;
    localparam bit [15:0] PORT_TELEM    = 16'd42002;  // telemetry push (1 Hz)
    localparam bit [15:0] PORT_MGMT     = 16'd9000;

    // ---- AXI-Stream widths ---------------------------------
    localparam int AXIS_DATA_W = 8;

    // ---- Order book operation ------------------------------
    // Carried in quote_t.op — tells the order book what to do
    // with the price/shares fields.
    typedef enum logic [1:0] {
        OP_ADD     = 2'b00,   // new resting order
        OP_CANCEL  = 2'b01,   // order cancelled
        OP_EXECUTE = 2'b10    // order executed (trade)
    } op_t;

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
    // cancel=1: emit OUCH 'X' (cancel) using only order_id field.
    // cancel=0: emit OUCH 'O' (new order) using all fields.
    typedef struct packed {
        logic [63:0]  order_id;
        logic [31:0]  price;       // fixed-point: price * 10000
        logic [31:0]  quantity;
        logic [15:0]  symbol_id;
        order_side_t  side;
        order_type_t  ord_type;
        logic         cancel;      // 1 = cancel order, 0 = new order
        logic         valid;
    } order_t;

    // ---- Market data quote (ITCH decoded → order book) -----
    // op field added in Phase 5: tells order book add/cancel/execute
    typedef struct packed {
        logic [63:0]  timestamp;
        logic [31:0]  price;
        logic [31:0]  shares;
        logic [15:0]  symbol_id;
        op_t          op;          // OP_ADD / OP_CANCEL / OP_EXECUTE
        logic         is_bid;
        logic         valid;
    } quote_t;

    // ---- Gateway MAC (ARP resolved in hardware; this is the hint) --
    localparam bit [47:0] GATEWAY_MAC = 48'hFF_FF_FF_FF_FF_FF; // broadcast fallback

    // ---- OUCH destination (PC running the order listener) ----------
    localparam bit [31:0] OUCH_DST_IP  = 32'hC0_A8_01_0B;   // 192.168.1.11

    // ---- ACK status codes ----------------------------------
    localparam bit [7:0] ACK_FILLED    = 8'h00;  // order fully filled
    localparam bit [7:0] ACK_PARTIAL   = 8'h01;  // partial fill, order still live
    localparam bit [7:0] ACK_REJECTED  = 8'h02;  // order rejected by exchange
    localparam bit [7:0] ACK_CANCELLED = 8'h03;  // cancel confirmed

    // ---- PHY reset hold time (10ms @ 125 MHz) --------------
    localparam int PHY_RESET_CYCLES = CLK_FREQ_HZ / 100;

    // ---- UART baud divisor (115200 @ 125 MHz) --------------
    localparam int UART_BAUD_DIV = CLK_FREQ_HZ / 115_200;

endpackage