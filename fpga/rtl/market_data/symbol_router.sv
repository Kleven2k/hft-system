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

    // Per-slot combinational routing: each slot independently checks symbol_id.
    // This form avoids icarus's "constant selects in always_*" limitation with
    // loop-indexed unpacked arrays.  Vivado synthesizes identically.
    generate
        for (genvar i = 0; i < N_BOOKS; i++) begin : gen_route
            always_comb begin
                if (quote_in.valid && (int'(quote_in.symbol_id) == i))
                    book[i] = quote_in;
                else
                    book[i] = '0;
            end
        end
    endgenerate

endmodule