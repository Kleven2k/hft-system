module eth_stack_wrapper #(
    parameter [47:0] LOCAL_MAC   = 48'h02_00_00_00_00_01,
    parameter [31:0] LOCAL_IP    = 32'hC0_A8_01_0A,
    parameter [31:0] SUBNET_MASK = 32'hFF_FF_FF_00,
    parameter [31:0] GATEWAY_IP  = 32'hC0_A8_01_01,
    parameter CLK_FREQ_HZ        = 125_000_000
)(
    input  wire clk,
    input  wire rst,

    // MAC RX
    input  wire [7:0] mac_rx_tdata,
    input  wire       mac_rx_tvalid,
    output wire       mac_rx_tready,
    input  wire       mac_rx_tlast,
    input  wire       mac_rx_tuser,

    // MAC TX
    output wire [7:0] mac_tx_tdata,
    output wire       mac_tx_tvalid,
    input  wire       mac_tx_tready,
    output wire       mac_tx_tlast,
    output wire       mac_tx_tuser,

    // UDP RX
    output wire [7:0] udp_rx_tdata,
    output wire       udp_rx_tvalid,
    input  wire       udp_rx_tready,
    output wire       udp_rx_tlast,
    output wire       udp_rx_tuser,

    output wire [15:0] udp_rx_src_port,
    output wire [15:0] udp_rx_dst_port,

    // UDP TX
    input  wire [7:0] udp_tx_tdata,
    input  wire       udp_tx_tvalid,
    output wire       udp_tx_tready,
    input  wire       udp_tx_tlast,
    input  wire       udp_tx_tuser,

    input  wire [47:0] udp_tx_dst_mac,
    input  wire [31:0] udp_tx_dst_ip,
    input  wire [15:0] udp_tx_src_port,
    input  wire [15:0] udp_tx_dst_port,
    input  wire [15:0] udp_tx_length
);

////////////////////////////////////////////////////////////////////////
// Ethernet RX wires
////////////////////////////////////////////////////////////////////////

wire [7:0]  eth_rx_payload_tdata;
wire        eth_rx_payload_tvalid;
wire        eth_rx_payload_tready;
wire        eth_rx_payload_tlast;
wire        eth_rx_payload_tuser;

wire [47:0] eth_rx_src_mac;
wire [47:0] eth_rx_dst_mac;
wire [15:0] eth_rx_type;

wire        eth_rx_hdr_valid;
wire        eth_rx_hdr_ready;

////////////////////////////////////////////////////////////////////////
// Ethernet TX wires
////////////////////////////////////////////////////////////////////////

wire [7:0]  eth_tx_payload_tdata;
wire        eth_tx_payload_tvalid;
wire        eth_tx_payload_tready;
wire        eth_tx_payload_tlast;
wire        eth_tx_payload_tuser;

wire [47:0] eth_tx_dst_mac;
wire [15:0] eth_tx_type;

wire        eth_tx_hdr_valid;
wire        eth_tx_hdr_ready;

////////////////////////////////////////////////////////////////////////
// UDP header handshake
////////////////////////////////////////////////////////////////////////

wire udp_tx_hdr_valid;
wire udp_tx_hdr_ready;

wire udp_rx_hdr_valid;
wire udp_rx_hdr_ready;

assign udp_tx_hdr_valid = udp_tx_tvalid;
assign udp_rx_hdr_ready = 1'b1;

////////////////////////////////////////////////////////////////////////
// Ethernet RX parser
////////////////////////////////////////////////////////////////////////

eth_axis_rx eth_rx_inst (
    .clk(clk),
    .rst(rst),

    .s_axis_tdata(mac_rx_tdata),
    .s_axis_tvalid(mac_rx_tvalid),
    .s_axis_tready(mac_rx_tready),
    .s_axis_tlast(mac_rx_tlast),
    .s_axis_tuser(mac_rx_tuser),

    .m_eth_hdr_valid(eth_rx_hdr_valid),
    .m_eth_hdr_ready(eth_rx_hdr_ready),

    .m_eth_dest_mac(eth_rx_dst_mac),
    .m_eth_src_mac(eth_rx_src_mac),
    .m_eth_type(eth_rx_type),

    .m_eth_payload_axis_tdata(eth_rx_payload_tdata),
    .m_eth_payload_axis_tvalid(eth_rx_payload_tvalid),
    .m_eth_payload_axis_tready(eth_rx_payload_tready),
    .m_eth_payload_axis_tlast(eth_rx_payload_tlast),
    .m_eth_payload_axis_tuser(eth_rx_payload_tuser)
);

////////////////////////////////////////////////////////////////////////
// Ethernet TX framer
////////////////////////////////////////////////////////////////////////

eth_axis_tx eth_tx_inst (
    .clk(clk),
    .rst(rst),

    .s_eth_hdr_valid(eth_tx_hdr_valid),
    .s_eth_hdr_ready(eth_tx_hdr_ready),

    .s_eth_dest_mac(eth_tx_dst_mac),
    .s_eth_src_mac(LOCAL_MAC),
    .s_eth_type(eth_tx_type),

    .s_eth_payload_axis_tdata(eth_tx_payload_tdata),
    .s_eth_payload_axis_tvalid(eth_tx_payload_tvalid),
    .s_eth_payload_axis_tready(eth_tx_payload_tready),
    .s_eth_payload_axis_tlast(eth_tx_payload_tlast),
    .s_eth_payload_axis_tuser(eth_tx_payload_tuser),

    .m_axis_tdata(mac_tx_tdata),
    .m_axis_tvalid(mac_tx_tvalid),
    .m_axis_tready(mac_tx_tready),
    .m_axis_tlast(mac_tx_tlast),
    .m_axis_tuser(mac_tx_tuser)
);

////////////////////////////////////////////////////////////////////////
// UDP + IP + ARP stack
////////////////////////////////////////////////////////////////////////

udp_complete udp_stack_inst (

    .clk(clk),
    .rst(rst),

    // ETH RX
    .s_eth_hdr_valid(eth_rx_hdr_valid),
    .s_eth_hdr_ready(eth_rx_hdr_ready),
    .s_eth_dest_mac(eth_rx_dst_mac),
    .s_eth_src_mac(eth_rx_src_mac),
    .s_eth_type(eth_rx_type),

    .s_eth_payload_axis_tdata(eth_rx_payload_tdata),
    .s_eth_payload_axis_tvalid(eth_rx_payload_tvalid),
    .s_eth_payload_axis_tready(eth_rx_payload_tready),
    .s_eth_payload_axis_tlast(eth_rx_payload_tlast),
    .s_eth_payload_axis_tuser(eth_rx_payload_tuser),

    // ETH TX
    .m_eth_hdr_valid(eth_tx_hdr_valid),
    .m_eth_hdr_ready(eth_tx_hdr_ready),
    .m_eth_dest_mac(eth_tx_dst_mac),
    .m_eth_src_mac(),
    .m_eth_type(eth_tx_type),

    .m_eth_payload_axis_tdata(eth_tx_payload_tdata),
    .m_eth_payload_axis_tvalid(eth_tx_payload_tvalid),
    .m_eth_payload_axis_tready(eth_tx_payload_tready),
    .m_eth_payload_axis_tlast(eth_tx_payload_tlast),
    .m_eth_payload_axis_tuser(eth_tx_payload_tuser),

    // UDP TX
    .s_udp_hdr_valid(udp_tx_hdr_valid),
    .s_udp_hdr_ready(udp_tx_hdr_ready),

    .s_udp_ip_source_ip(LOCAL_IP),
    .s_udp_ip_dest_ip(udp_tx_dst_ip),

    .s_udp_source_port(udp_tx_src_port),
    .s_udp_dest_port(udp_tx_dst_port),
    .s_udp_length(udp_tx_length),
    .s_udp_checksum(16'd0),

    .s_udp_payload_axis_tdata(udp_tx_tdata),
    .s_udp_payload_axis_tvalid(udp_tx_tvalid),
    .s_udp_payload_axis_tready(udp_tx_tready),
    .s_udp_payload_axis_tlast(udp_tx_tlast),
    .s_udp_payload_axis_tuser(udp_tx_tuser),

    // UDP RX
    .m_udp_hdr_valid(udp_rx_hdr_valid),
    .m_udp_hdr_ready(udp_rx_hdr_ready),

    .m_udp_source_port(udp_rx_src_port),
    .m_udp_dest_port(udp_rx_dst_port),

    .m_udp_payload_axis_tdata(udp_rx_tdata),
    .m_udp_payload_axis_tvalid(udp_rx_tvalid),
    .m_udp_payload_axis_tready(udp_rx_tready),
    .m_udp_payload_axis_tlast(udp_rx_tlast),
    .m_udp_payload_axis_tuser(udp_rx_tuser),

    // Non-UDP IP output — drain immediately (TCP/ICMP frames must not stall udp_complete)
    .m_ip_payload_axis_tdata(),
    .m_ip_payload_axis_tvalid(),
    .m_ip_payload_axis_tready(1'b1),
    .m_ip_payload_axis_tlast(),
    .m_ip_payload_axis_tuser(),

    // Config
    .local_mac(LOCAL_MAC),
    .local_ip(LOCAL_IP),
    .gateway_ip(GATEWAY_IP),
    .subnet_mask(SUBNET_MASK),
    .clear_arp_cache(1'b0)
);

endmodule