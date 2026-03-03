// ============================================================
// eth_stack_wrapper.sv
// Wraps verilog-ethernet ARP/IP/UDP stack.
// This is the only file that instantiates verilog-ethernet
// modules directly — isolates the rest of the design from
// upstream API changes and board-to-board differences.
// ============================================================
module eth_stack_wrapper
    import hft_pkg::*;
(
    input  logic    clk,
    input  logic    rst,

    // To/from MAC (connects to eth_mac_1g_rgmii_fifo in top)
    axis_if.slave   mac_rx,
    axis_if.master  mac_tx,

    // UDP RX payload → market_data_parser
    axis_if.master  udp_rx,
    output logic [15:0] udp_rx_src_port,
    output logic [15:0] udp_rx_dst_port,

    // UDP TX payload ← order_engine
    axis_if.slave   udp_tx,
    input  logic [47:0] udp_tx_dst_mac,
    input  logic [31:0] udp_tx_dst_ip,
    input  logic [15:0] udp_tx_src_port,
    input  logic [15:0] udp_tx_dst_port,
    input  logic [15:0] udp_tx_length
);

    // ---- Internal ETH frame buses --------------------------
    // verilog-ethernet modules are plain Verilog — we can't
    // pass interfaces to them, so we unpack here at the boundary.
    logic [7:0] eth_rx_payload_tdata;
    logic       eth_rx_payload_tvalid;
    logic       eth_rx_payload_tready;
    logic       eth_rx_payload_tlast;
    logic       eth_rx_payload_tuser;
    logic [47:0] eth_rx_src_mac, eth_rx_dst_mac;
    logic [15:0] eth_rx_type;

    logic [7:0] eth_tx_payload_tdata;
    logic       eth_tx_payload_tvalid;
    logic       eth_tx_payload_tready;
    logic       eth_tx_payload_tlast;
    logic       eth_tx_payload_tuser;
    logic [47:0] eth_tx_dst_mac;
    logic [15:0] eth_tx_type;

    // ---- ETH frame decode ----------------------------------
    eth_axis_rx eth_axis_rx_inst (
        .clk                            (clk),
        .rst                            (rst),
        // Unpack mac_rx interface
        .s_axis_tdata                   (mac_rx.tdata),
        .s_axis_tvalid                  (mac_rx.tvalid),
        .s_axis_tready                  (mac_rx.tready),
        .s_axis_tlast                   (mac_rx.tlast),
        .s_axis_tuser                   (mac_rx.tuser),
        // ETH fields out
        .m_eth_payload_axis_tdata       (eth_rx_payload_tdata),
        .m_eth_payload_axis_tvalid      (eth_rx_payload_tvalid),
        .m_eth_payload_axis_tready      (eth_rx_payload_tready),
        .m_eth_payload_axis_tlast       (eth_rx_payload_tlast),
        .m_eth_payload_axis_tuser       (eth_rx_payload_tuser),
        .m_eth_dest_mac                 (eth_rx_dst_mac),
        .m_eth_src_mac                  (eth_rx_src_mac),
        .m_eth_type                     (eth_rx_type)
    );

    eth_axis_tx eth_axis_tx_inst (
        .clk                            (clk),
        .rst                            (rst),
        .s_eth_payload_axis_tdata       (eth_tx_payload_tdata),
        .s_eth_payload_axis_tvalid      (eth_tx_payload_tvalid),
        .s_eth_payload_axis_tready      (eth_tx_payload_tready),
        .s_eth_payload_axis_tlast       (eth_tx_payload_tlast),
        .s_eth_payload_axis_tuser       (eth_tx_payload_tuser),
        .s_eth_dest_mac                 (eth_tx_dst_mac),
        .s_eth_src_mac                  (LOCAL_MAC),
        .s_eth_type                     (eth_tx_type),
        // Pack mac_tx interface
        .m_axis_tdata                   (mac_tx.tdata),
        .m_axis_tvalid                  (mac_tx.tvalid),
        .m_axis_tready                  (mac_tx.tready),
        .m_axis_tlast                   (mac_tx.tlast),
        .m_axis_tuser                   (mac_tx.tuser)
    );

    // ---- ARP + IP + UDP ------------------------------------
    udp_complete #(
        .ARP_CACHE_ADDR_WIDTH           (2),         // 4-entry ARP cache
        .ARP_REQUEST_RETRY_COUNT        (4),
        .ARP_REQUEST_RETRY_INTERVAL     (CLK_FREQ_HZ),     // 1s
        .ARP_REQUEST_TIMEOUT            (CLK_FREQ_HZ * 5), // 5s
        .UDP_CHECKSUM_GEN_ENABLE        (0),         // off for latency
        .UDP_CHECKSUM_PAYLOAD_FIFO_DEPTH(2048)
    ) udp_complete_inst (
        .clk                            (clk),
        .rst                            (rst),
        // ETH RX
        .s_eth_payload_axis_tdata       (eth_rx_payload_tdata),
        .s_eth_payload_axis_tvalid      (eth_rx_payload_tvalid),
        .s_eth_payload_axis_tready      (eth_rx_payload_tready),
        .s_eth_payload_axis_tlast       (eth_rx_payload_tlast),
        .s_eth_payload_axis_tuser       (eth_rx_payload_tuser),
        .s_eth_dest_mac                 (eth_rx_dst_mac),
        .s_eth_src_mac                  (eth_rx_src_mac),
        .s_eth_type                     (eth_rx_type),
        // ETH TX
        .m_eth_payload_axis_tdata       (eth_tx_payload_tdata),
        .m_eth_payload_axis_tvalid      (eth_tx_payload_tvalid),
        .m_eth_payload_axis_tready      (eth_tx_payload_tready),
        .m_eth_payload_axis_tlast       (eth_tx_payload_tlast),
        .m_eth_payload_axis_tuser       (eth_tx_payload_tuser),
        .m_eth_dest_mac                 (eth_tx_dst_mac),
        .m_eth_src_mac                  (),
        .m_eth_type                     (eth_tx_type),
        // Config
        .local_mac                      (LOCAL_MAC),
        .local_ip                       (LOCAL_IP),
        .gateway_ip                     (GATEWAY_IP),
        .subnet_mask                    (SUBNET_MASK),
        // UDP RX → application (unpack to udp_rx interface)
        .m_udp_payload_axis_tdata       (udp_rx.tdata),
        .m_udp_payload_axis_tvalid      (udp_rx.tvalid),
        .m_udp_payload_axis_tready      (udp_rx.tready),
        .m_udp_payload_axis_tlast       (udp_rx.tlast),
        .m_udp_payload_axis_tuser       (udp_rx.tuser),
        .m_udp_src_port                 (udp_rx_src_port),
        .m_udp_dest_port                (udp_rx_dst_port),
        // UDP TX ← application (unpack from udp_tx interface)
        .s_udp_payload_axis_tdata       (udp_tx.tdata),
        .s_udp_payload_axis_tvalid      (udp_tx.tvalid),
        .s_udp_payload_axis_tready      (udp_tx.tready),
        .s_udp_payload_axis_tlast       (udp_tx.tlast),
        .s_udp_payload_axis_tuser       (udp_tx.tuser),
        .s_udp_ip_dscp                  (6'd0),
        .s_udp_ip_ecn                   (2'd0),
        .s_udp_ip_ttl                   (8'd64),
        .s_udp_ip_source_ip             (LOCAL_IP),
        .s_udp_ip_dest_ip               (udp_tx_dst_ip),
        .s_udp_src_port                 (udp_tx_src_port),
        .s_udp_dest_port                (udp_tx_dst_port),
        .s_udp_length                   (udp_tx_length),
        .s_udp_checksum                 (16'd0)
    );

endmodule