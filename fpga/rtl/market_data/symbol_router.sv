// ============================================================
// symbol_router.sv
// Routes quote to one of N_BOOKS order book slots.
// symbol_id is 16-bit but we only support N_BOOKS slots here.
// Unknown symbol_id → all outputs invalid.
// ============================================================
module symbol_router
    import hft_pkg::*;
#(
    parameter int N_BOOKS = 4
)
(
    input  quote_t quote_in,
    output quote_t book [0:N_BOOKS-1]
);

    always_comb begin
        for (int i = 0; i < N_BOOKS; i++)
            book[i] = '0;

        if (quote_in.valid && quote_in.symbol_id < N_BOOKS)
            book[quote_in.symbol_id] = quote_in;
    end

endmodule