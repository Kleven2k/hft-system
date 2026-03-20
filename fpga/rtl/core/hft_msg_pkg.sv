// ============================================================
// hft_msg_pkg.sv
// Fixed-width message serialization for async FIFO CDC crossing.
//
// Flat bit layout (FIFO_WIDTH = 147 bits), unambiguous:
//   [146:145]  op         (2 bits: OP_ADD/OP_CANCEL/OP_EXECUTE)
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

    localparam int FIFO_WIDTH = 147;

    // ---- Encode: explicit field extraction (no struct packing ambiguity) ----
    function automatic logic [FIFO_WIDTH-1:0] encode_quote(
        input quote_t q
    );
        logic [FIFO_WIDTH-1:0] w;
        w[146:145] = q.op;
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
        q.op        = op_t'(w[146:145]);
        q.is_bid    = w[144];
        q.timestamp = w[143:80];
        q.price     = w[79:48];
        q.shares    = w[47:16];
        q.symbol_id = w[15:0];
        return q;
    endfunction


    // ---- Order encoding for clk→rgmii_rxc CDC FIFO ---------
    // Flat bit layout (ORDER_FIFO_WIDTH = 149 bits):
    //   [148:85]  order_id  (64 bits)
    //   [84:53]   price     (32 bits)
    //   [52:21]   quantity  (32 bits)
    //   [20:5]    symbol_id (16 bits)
    //   [4:3]     side      (2 bits)
    //   [2:1]     ord_type  (2 bits)
    //   [0]       cancel    (1 bit: 1=cancel, 0=new order)
    // Note: 'valid' excluded — anything exiting FIFO is valid.
    localparam int ORDER_FIFO_WIDTH = 149;

    function automatic logic [ORDER_FIFO_WIDTH-1:0] encode_order(
        input order_t o
    );
        logic [ORDER_FIFO_WIDTH-1:0] w;
        w[148:85] = o.order_id;
        w[84:53]  = o.price;
        w[52:21]  = o.quantity;
        w[20:5]   = o.symbol_id;
        w[4:3]    = o.side;
        w[2:1]    = o.ord_type;
        w[0]      = o.cancel;
        return w;
    endfunction

    function automatic order_t decode_order(
        input logic [ORDER_FIFO_WIDTH-1:0] w
    );
        order_t o;
        o.valid    = 1'b1;
        o.order_id = w[148:85];
        o.price    = w[84:53];
        o.quantity = w[52:21];
        o.symbol_id= w[20:5];
        o.side     = order_side_t'(w[4:3]);
        o.ord_type = order_type_t'(w[2:1]);
        o.cancel   = w[0];
        return o;
    endfunction

endpackage