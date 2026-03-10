// ============================================================
// hft_msg_pkg.sv
// Fixed-width message serialization for async FIFO CDC crossing.
//
// Flat bit layout (FIFO_WIDTH = 145 bits), unambiguous:
//   [144]      is_bid
//   [143:80]   timestamp  (64 bits)
//   [79:48]    price      (32 bits)
//   [47:16]    shares     (32 bits)
//   [15:0]     symbol_id  (16 bits)
//
// Note: 'valid' is NOT encoded — anything that exits the FIFO
//       is by definition valid. The decode function sets valid=1.
// ============================================================

package hft_msg_pkg;

    import hft_pkg::*;

    localparam int FIFO_WIDTH = 145;

    // ---- Encode: explicit field extraction (no struct packing ambiguity) ----
    function automatic logic [FIFO_WIDTH-1:0] encode_quote(
        input quote_t q
    );
        logic [FIFO_WIDTH-1:0] w;
        w[144]     = q.is_bid;
        w[143:80]  = q.timestamp;
        w[79:48]   = q.price;
        w[47:16]   = q.shares;
        w[15:0]    = q.symbol_id;
        return w;
    endfunction

    // ---- Decode: explicit field assignment --------------------------------
    function automatic quote_t decode_quote(
        input logic [FIFO_WIDTH-1:0] w
    );
        quote_t q;
        q.valid     = 1'b1;
        q.is_bid    = w[144];
        q.timestamp = w[143:80];
        q.price     = w[79:48];
        q.shares    = w[47:16];
        q.symbol_id = w[15:0];
        return q;
    endfunction


    // ---- Order encoding for clk→rgmii_rxc CDC FIFO ---------
    // Flat bit layout (ORDER_FIFO_WIDTH = 148 bits):
    //   [147:84]  order_id  (64 bits)
    //   [83:52]   price     (32 bits)
    //   [51:20]   quantity  (32 bits)
    //   [19:4]    symbol_id (16 bits)
    //   [3:2]     side      (2 bits)
    //   [1:0]     ord_type  (2 bits)
    // Note: 'valid' excluded — anything exiting FIFO is valid.
    localparam int ORDER_FIFO_WIDTH = 148;

    function automatic logic [ORDER_FIFO_WIDTH-1:0] encode_order(
        input order_t o
    );
        logic [ORDER_FIFO_WIDTH-1:0] w;
        w[147:84] = o.order_id;
        w[83:52]  = o.price;
        w[51:20]  = o.quantity;
        w[19:4]   = o.symbol_id;
        w[3:2]    = o.side;
        w[1:0]    = o.ord_type;
        return w;
    endfunction

    function automatic order_t decode_order(
        input logic [ORDER_FIFO_WIDTH-1:0] w
    );
        order_t o;
        o.valid    = 1'b1;
        o.order_id = w[147:84];
        o.price    = w[83:52];
        o.quantity = w[51:20];
        o.symbol_id= w[19:4];
        o.side     = order_side_t'(w[3:2]);
        o.ord_type = order_type_t'(w[1:0]);
        return o;
    endfunction

endpackage