// ============================================================
// order_book.sv
// Single-symbol order book.
//
// MAX_PRICE=1024 → 2x RAMB36 on Artix-7.
// Price field from quote_t is 32-bit but we index only the
// lower $clog2(MAX_PRICE) bits — caller must ensure prices
// are pre-scaled to fit.
// ============================================================
module order_book
    import hft_pkg::*;
#(
    parameter int MAX_PRICE = 1024
)
(
    input  logic        clk,
    input  logic        rst,
    input  quote_t      quote_in,

    output logic [31:0] best_bid_price,
    output logic [31:0] best_ask_price,
    output logic [31:0] best_bid_size,
    output logic [31:0] best_ask_size
);

    localparam int PRICE_BITS = $clog2(MAX_PRICE);

    // ---- Book storage — inferred as BRAM -------------------
    logic [31:0] bid_book [0:MAX_PRICE-1];
    logic [31:0] ask_book [0:MAX_PRICE-1];

    // ---- Best price tracking -------------------------------
    logic [31:0] best_bid;
    logic [31:0] best_ask;
    logic        bid_valid;
    logic        ask_valid;

    // ---- Price index (truncated to book depth) -------------
    logic [PRICE_BITS-1:0] price_idx;
    assign price_idx = quote_in.price[PRICE_BITS-1:0];

    // ---- Update --------------------------------------------
    always_ff @(posedge clk) begin
        if (rst) begin
            best_bid  <= '0;
            best_ask  <= '0;
            bid_valid <= 0;
            ask_valid <= 0;
        end
        else if (quote_in.valid) begin

            if (quote_in.is_bid) begin
                bid_book[price_idx] <= bid_book[price_idx] + quote_in.shares;
                if (!bid_valid || quote_in.price > best_bid) begin
                    best_bid  <= quote_in.price;
                    bid_valid <= 1;
                end
            end
            else begin
                ask_book[price_idx] <= ask_book[price_idx] + quote_in.shares;
                if (!ask_valid || quote_in.price < best_ask) begin
                    best_ask  <= quote_in.price;
                    ask_valid <= 1;
                end
            end

        end
    end

    // ---- Outputs -------------------------------------------
    assign best_bid_price = bid_valid ? best_bid  : '0;
    assign best_ask_price = ask_valid ? best_ask  : '0;
    assign best_bid_size  = bid_valid ? bid_book[best_bid[PRICE_BITS-1:0]] : '0;
    assign best_ask_size  = ask_valid ? ask_book[best_ask[PRICE_BITS-1:0]] : '0;

endmodule