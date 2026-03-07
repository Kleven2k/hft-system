

module best_bid_tracker
(
    input  logic clk,
    input  logic rst,

    input  logic price_add,
    input  logic price_remove,
    input  logic [15:0] price,

    output logic [15:0] best_bid
);

    parameter MAX_PRICE = 65536;

    logic price_bitmap [0:MAX_PRICE-1];

    always_ff @(posedge clk) begin

        if (price_add)
            price_bitmap[price] <= 1;

        if (price_remove)
            price_bitmap[price] <= 0;

    end

    integer i;

    always_comb begin

        best_bid = 0;

        for (i = MAX_PRICE-1; i >= 0; i--) begin
            if (price_bitmap[i]) begin
                best_bid = i;
                break;
            end
        end

    end

endmodule