// ============================================================
// order_engine.sv — Top-level order entry wrapper
//
// Connects:
//   strategy (clk domain)
//     → order_cdc_bridge (clk → rgmii_rxc gray-code FIFO)
//       → ouch_encoder (rgmii_rxc domain, UDP AXI-S output)
//
// Phase 12 — ACK feedback path (toggle CDC, rgmii_rxc → clk).
// Phase 15 — OMS: 64-bit order_id + fill_qty through CDC.
// Phase 16 — Pre-trade risk: kill_switch + mid_price passed to
//            strategy; FAT_FINGER_BPS, MAX_BURST, REFILL_PERIOD
//            parameters forwarded.
// Phase 17 — Inventory skew: SKEW_SHIFT parameter forwarded.
// Phase 20 — Telemetry: telemetry_tx (clk domain) →
//            axis_async_fifo (CDC) → udp_tx (telemetry only).
// Phase 27 — OUCH 4.2: ouch_encoder output exposed as ouch_tx_*
//            ports (no longer goes to UDP). soup_session wraps
//            OUCH bytes in SoupBinTCP and forwards to tcp_engine.
// ============================================================
`timescale 1ns/1ps
module order_engine
    import hft_pkg::*;
#(
    parameter int N_BOOKS        = 4,
    parameter int SPREAD_MAX     = 20000,
    parameter int ORDER_QTY      = 100,
    parameter int MAX_POSITION   = 1000,
    parameter int COOLDOWN_CYC   = 12_500_000,
    parameter int SKEW_SHIFT     = 31,
    parameter int FAT_FINGER_BPS = 500,
    parameter int MAX_BURST         = 5,
    parameter int REFILL_PERIOD     = 12_500_000,
    parameter int SPREAD_EMA_SHIFT  = 4,
    parameter int QUOTE_OFFSET      = 3,          // power-on default per slot
    parameter int STALE_MULT        = 3,          // stale_thresh = quote_offset × STALE_MULT
    parameter int TIMEOUT_CYC       = 1_250_000_000,  // 10 s safety timeout
    // OUCH 4.2 stock symbols (6-byte ASCII per slot)
    parameter [47:0] STOCK_0    = 48'h41_41_50_4C_20_20,  // "AAPL  "
    parameter [47:0] STOCK_1    = 48'h4D_53_46_54_20_20,  // "MSFT  "
    parameter [47:0] STOCK_2    = 48'h41_4D_5A_4E_20_20,  // "AMZN  "
    parameter [47:0] STOCK_3    = 48'h54_53_4C_41_20_20,  // "TSLA  "
    parameter [47:0] FIRM       = 48'h4D_59_46_52_4D_20,  // "MYFIRM"
    parameter [7:0]  CAPACITY   = 8'h41                   // 'A' agency
)(
    // ---- Strategy clock domain --------------------------------
    input  logic        clk,
    input  logic        rst,
    input  logic        rst_n,

    // Order book signals (clk domain)
    input  logic [31:0] best_bid_price [0:N_BOOKS-1],
    input  logic [31:0] best_ask_price [0:N_BOOKS-1],
    input  logic [31:0] mid_price      [0:N_BOOKS-1],
    input  logic [31:0] spread         [0:N_BOOKS-1],
    input  logic        bid_valid      [0:N_BOOKS-1],
    input  logic        ask_valid      [0:N_BOOKS-1],

    // Per-slot quote offset (UART-configurable)
    input  logic [31:0] quote_offset   [0:N_BOOKS-1],

    // Pre-trade risk
    input  logic        kill_switch,

    // ---- Encoder clock domain ---------------------------------
    input  logic        enc_clk,
    input  logic        enc_rst_n,

    // OUCH 4.2 AXI-Stream output (enc_clk domain) → soup_session → tcp_engine
    output logic [7:0]  ouch_tx_tdata,
    output logic        ouch_tx_tvalid,
    input  logic        ouch_tx_tready,
    output logic        ouch_tx_tlast,

    // UDP TX AXI-Stream — telemetry only (rgmii_rxc domain)
    output logic [7:0]  udp_tx_tdata,
    output logic        udp_tx_tvalid,
    input  logic        udp_tx_tready,
    output logic        udp_tx_tlast,
    output logic        udp_tx_tuser,

    // UDP TX sideband (rgmii_rxc domain)
    output logic [47:0] udp_tx_dst_mac,
    output logic [31:0] udp_tx_dst_ip,
    output logic [15:0] udp_tx_src_port,
    output logic [15:0] udp_tx_dst_port,
    output logic [15:0] udp_tx_length,

    // ACK inputs from ouch_ack_receiver (rgmii_rxc domain)
    input  logic        ack_raw_valid,
    input  logic [63:0] ack_raw_order_id,
    input  logic [7:0]  ack_raw_status,
    input  logic [31:0] ack_raw_fill_qty,

    // Phase 25 — latency stats from latency_monitor (clk domain)
    input  logic [31:0] lat_min,
    input  logic [31:0] lat_max,
    input  logic [31:0] lat_last,
    input  logic [31:0] lat_count,

    // Debug
    output logic [15:0] drop_count,

    // Phase 25 — latency measurement pulse (clk domain)
    output logic        order_sent    // 1-cycle pulse when strategy emits an order
);

    // ---- ACK toggle CDC (rgmii_rxc → clk) --------------------
    logic        ack_toggle;
    logic [63:0] ack_data_id;
    logic [7:0]  ack_data_status;
    logic [31:0] ack_data_fill_qty;

    always_ff @(posedge enc_clk or negedge enc_rst_n) begin
        if (!enc_rst_n) begin
            ack_toggle        <= 1'b0;
            ack_data_id       <= '0;
            ack_data_status   <= '0;
            ack_data_fill_qty <= '0;
        end else if (ack_raw_valid) begin
            ack_data_id       <= ack_raw_order_id;
            ack_data_status   <= ack_raw_status;
            ack_data_fill_qty <= ack_raw_fill_qty;
            ack_toggle        <= ~ack_toggle;
        end
    end

    logic [1:0] ack_sync;
    logic       ack_sync_prev;

    logic        strat_ack_valid;
    logic [63:0] strat_ack_order_id;
    logic [7:0]  strat_ack_status;
    logic [31:0] strat_ack_fill_qty;

    always_ff @(posedge clk) begin
        if (rst) begin
            ack_sync        <= 2'b00;
            ack_sync_prev   <= 1'b0;
            strat_ack_valid <= 1'b0;
        end else begin
            ack_sync      <= {ack_sync[0], ack_toggle};
            ack_sync_prev <= ack_sync[1];
            strat_ack_valid <= (ack_sync[1] != ack_sync_prev);
            if (ack_sync[1] != ack_sync_prev) begin
                strat_ack_order_id <= ack_data_id;
                strat_ack_status   <= ack_data_status;
                strat_ack_fill_qty <= ack_data_fill_qty;
            end
        end
    end

    // ---- Strategy (clk domain) --------------------------------
    order_t strat_order;
    logic   strat_valid;

    // Phase 25 — expose strat_valid as order_sent pulse
    assign order_sent = strat_valid;

    logic signed [31:0] strat_position   [0:N_BOOKS-1];
    logic signed [31:0] strat_pnl        [0:N_BOOKS-1];
    logic        [7:0]  strat_reject_cnt [0:N_BOOKS-1];
    logic        [7:0]  strat_token_cnt  [0:N_BOOKS-1];
    logic               strat_bid_valid  [0:N_BOOKS-1];
    logic               strat_ask_valid  [0:N_BOOKS-1];
    logic        [63:0] strat_order_id_cnt;

    strategy #(
        .N_BOOKS       (N_BOOKS),
        .SPREAD_MAX    (SPREAD_MAX),
        .ORDER_QTY     (ORDER_QTY),
        .MAX_POSITION  (MAX_POSITION),
        .COOLDOWN_CYC  (COOLDOWN_CYC),
        .TIMEOUT_CYC   (TIMEOUT_CYC),
        .SKEW_SHIFT    (SKEW_SHIFT),
        .FAT_FINGER_BPS  (FAT_FINGER_BPS),
        .MAX_BURST       (MAX_BURST),
        .REFILL_PERIOD   (REFILL_PERIOD),
        .SPREAD_EMA_SHIFT(SPREAD_EMA_SHIFT),
        .STALE_MULT      (STALE_MULT)
    ) u_strategy (
        .clk              (clk),
        .rst              (rst),
        .best_bid_price   (best_bid_price),
        .best_ask_price   (best_ask_price),
        .mid_price        (mid_price),
        .spread           (spread),
        .bid_valid        (bid_valid),
        .ask_valid        (ask_valid),
        .quote_offset     (quote_offset),
        .kill_switch      (kill_switch),
        .ack_valid        (strat_ack_valid),
        .ack_order_id     (strat_ack_order_id),
        .ack_status       (strat_ack_status),
        .ack_fill_qty     (strat_ack_fill_qty),
        .order_out        (strat_order),
        .order_valid      (strat_valid),
        .telem_position    (strat_position),
        .telem_pnl         (strat_pnl),
        .telem_reject_cnt  (strat_reject_cnt),
        .telem_token_cnt   (strat_token_cnt),
        .telem_bid_valid   (strat_bid_valid),
        .telem_ask_valid   (strat_ask_valid),
        .telem_order_id_cnt(strat_order_id_cnt)
    );

    // ---- CDC bridge (clk → rgmii_rxc) for OUCH orders --------
    order_t enc_order;
    logic   enc_order_valid;

    order_cdc_bridge u_cdc (
        .wclk         (clk),
        .wrst_n       (rst_n),
        .w_order      (strat_order),
        .w_order_valid(strat_valid),
        .w_drop       (),
        .w_drop_count (drop_count),
        .rclk         (enc_clk),
        .rrst_n       (enc_rst_n),
        .r_order      (enc_order),
        .r_order_valid(enc_order_valid),
        .fifo_empty   (),
        .fifo_full_rclk()
    );

    // ---- OUCH 4.2 encoder (rgmii_rxc domain) → ouch_tx_* ports ----
    ouch_encoder #(
        .STOCK_0  (STOCK_0),
        .STOCK_1  (STOCK_1),
        .STOCK_2  (STOCK_2),
        .STOCK_3  (STOCK_3),
        .FIRM     (FIRM),
        .CAPACITY (CAPACITY)
    ) u_encoder (
        .clk         (enc_clk),
        .rst         (~enc_rst_n),
        .order_in    (enc_order),
        .order_valid (enc_order_valid),
        .tx_tdata    (ouch_tx_tdata),
        .tx_tvalid   (ouch_tx_tvalid),
        .tx_tready   (ouch_tx_tready),
        .tx_tlast    (ouch_tx_tlast)
    );

    // ---- Telemetry TX (clk domain) ----------------------------
    logic [7:0]  telem_tdata_clk;
    logic        telem_tvalid_clk;
    logic        telem_tready_clk;
    logic        telem_tlast_clk;

    telemetry_tx #(
        .N_BOOKS      (N_BOOKS),
        .TELEM_PERIOD (CLK_FREQ_HZ)
    ) u_telemetry (
        .clk         (clk),
        .rst         (rst),
        .position    (strat_position),
        .pnl         (strat_pnl),
        .reject_cnt  (strat_reject_cnt),
        .token_cnt   (strat_token_cnt),
        .bid_valid   (strat_bid_valid),
        .ask_valid   (strat_ask_valid),
        .order_id_cnt(strat_order_id_cnt),
        .lat_min     (lat_min),
        .lat_max     (lat_max),
        .lat_last    (lat_last),
        .lat_count   (lat_count),
        .tx_tdata    (telem_tdata_clk),
        .tx_tvalid   (telem_tvalid_clk),
        .tx_tready   (telem_tready_clk),
        .tx_tlast    (telem_tlast_clk),
        .tx_tuser    (),
        .tx_dst_mac  (),
        .tx_dst_ip   (),
        .tx_src_port (),
        .tx_dst_port (),
        .tx_length   ()
    );

    // ---- Async FIFO: telemetry clk → enc_clk (rgmii_rxc) -----
    logic [7:0]  telem_tdata_enc;
    logic        telem_tvalid_enc;
    logic        telem_tready_enc;
    logic        telem_tlast_enc;

    axis_async_fifo #(
        .DEPTH        (128),
        .DATA_WIDTH   (8),
        .KEEP_ENABLE  (0),
        .LAST_ENABLE  (1),
        .ID_ENABLE    (0),
        .DEST_ENABLE  (0),
        .USER_ENABLE  (0),
        .RAM_PIPELINE (1),
        .FRAME_FIFO   (0)
    ) u_telem_fifo (
        .s_clk               (clk),
        .s_rst               (rst),
        .s_axis_tdata        (telem_tdata_clk),
        .s_axis_tkeep        (1'b1),
        .s_axis_tvalid       (telem_tvalid_clk),
        .s_axis_tready       (telem_tready_clk),
        .s_axis_tlast        (telem_tlast_clk),
        .s_axis_tid          ('0),
        .s_axis_tdest        ('0),
        .s_axis_tuser        ('0),
        .s_pause_req         (1'b0),
        .s_pause_ack         (),
        .m_clk               (enc_clk),
        .m_rst               (~enc_rst_n),
        .m_axis_tdata        (telem_tdata_enc),
        .m_axis_tkeep        (),
        .m_axis_tvalid       (telem_tvalid_enc),
        .m_axis_tready       (telem_tready_enc),
        .m_axis_tlast        (telem_tlast_enc),
        .m_axis_tid          (),
        .m_axis_tdest        (),
        .m_axis_tuser        (),
        .m_pause_req         (1'b0),
        .m_pause_ack         (),
        .s_status_depth      (),
        .s_status_depth_commit(),
        .s_status_overflow   (),
        .s_status_bad_frame  (),
        .s_status_good_frame (),
        .m_status_depth      (),
        .m_status_depth_commit(),
        .m_status_overflow   (),
        .m_status_bad_frame  (),
        .m_status_good_frame ()
    );

    // ---- UDP TX: telemetry passthrough (enc_clk domain) -------
    // OUCH orders now go to ouch_tx_* ports (→ soup_session → tcp_engine).
    assign telem_tready_enc = udp_tx_tready;
    assign udp_tx_tdata     = telem_tdata_enc;
    assign udp_tx_tvalid    = telem_tvalid_enc;
    assign udp_tx_tlast     = telem_tlast_enc;
    assign udp_tx_tuser     = 1'b0;
    assign udp_tx_dst_mac   = GATEWAY_MAC;
    assign udp_tx_dst_ip    = OUCH_DST_IP;
    assign udp_tx_src_port  = PORT_OUCH;
    assign udp_tx_dst_port  = PORT_TELEM;
    assign udp_tx_length    = 16'd80;   // Phase 25: 80-byte telemetry frame

endmodule
