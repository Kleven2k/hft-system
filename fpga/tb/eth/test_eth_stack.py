# ============================================================
# test_eth_stack.py
# cocotb + Scapy Ethernet host simulation
# ============================================================

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from cocotb.utils import get_sim_time

from scapy.all import Ether, ARP, IP, UDP, raw

from nic_model import SimulatedNIC


# ============================================================
# NETWORK CONSTANTS
# ============================================================

LOCAL_MAC  = "02:00:00:00:00:01"
LOCAL_IP   = "192.168.1.10"

CLIENT_MAC = "aa:bb:cc:dd:ee:ff"
CLIENT_IP  = "192.168.1.11"

ITCH_PORT  = 17010


# ============================================================
# AXI STREAM HELPERS
# ============================================================

async def axis_send(clk, tdata, tvalid, tready, tlast, tuser, payload):

    for i, b in enumerate(payload):

        tdata.value  = b
        tvalid.value = 1
        tlast.value  = (i == len(payload) - 1)
        tuser.value  = 0

        await RisingEdge(clk)

        while not tready.value:
            await RisingEdge(clk)

    tvalid.value = 0
    tlast.value  = 0


async def axis_recv(clk, tdata, tvalid, tready, tlast, timeout=10000):

    result = []
    cycles = 0

    tready.value = 1

    while True:

        await RisingEdge(clk)
        cycles += 1

        assert cycles <= timeout, "axis_recv timeout"

        if tvalid.value:

            result.append(int(tdata.value))

            if tlast.value:
                return bytes(result)


# ============================================================
# SCAPY PACKET BUILDERS
# ============================================================

def scapy_arp_request():

    pkt = Ether(
        src=CLIENT_MAC,
        dst="ff:ff:ff:ff:ff:ff"
    ) / ARP(
        op=1,
        hwsrc=CLIENT_MAC,
        psrc=CLIENT_IP,
        hwdst="00:00:00:00:00:00",
        pdst=LOCAL_IP
    )

    return raw(pkt)


def scapy_arp_reply():

    pkt = Ether(
        src=CLIENT_MAC,
        dst=LOCAL_MAC
    ) / ARP(
        op=2,
        hwsrc=CLIENT_MAC,
        psrc=CLIENT_IP,
        hwdst=LOCAL_MAC,
        pdst=LOCAL_IP
    )

    return raw(pkt)


def scapy_udp(payload):

    pkt = Ether(
        src=CLIENT_MAC,
        dst=LOCAL_MAC
    ) / IP(
        src=CLIENT_IP,
        dst=LOCAL_IP
    ) / UDP(
        sport=12345,
        dport=ITCH_PORT
    ) / payload

    return raw(pkt)


# ============================================================
# DEBUG DECODER
# ============================================================

def decode_frame(frame):

    pkt = Ether(frame)

    print("\n--- Packet ---")
    print(pkt.summary())
    pkt.show()


# ============================================================
# RESET
# ============================================================

async def do_reset(dut):

    dut.rst.value = 1

    for _ in range(20):
        await RisingEdge(dut.clk)

    dut.rst.value = 0

    await RisingEdge(dut.clk)


# ============================================================
# ARP HANDSHAKE
# ============================================================

async def arp_handshake(dut):

    # start receiver BEFORE sending packet
    recv_task = cocotb.start_soon(axis_recv(
        dut.clk,
        dut.mac_tx_tdata,
        dut.mac_tx_tvalid,
        dut.mac_tx_tready,
        dut.mac_tx_tlast
    ))

    await axis_send(
        dut.clk,
        dut.mac_rx_tdata,
        dut.mac_rx_tvalid,
        dut.mac_rx_tready,
        dut.mac_rx_tlast,
        dut.mac_rx_tuser,
        scapy_arp_request()
    )

    frame = await recv_task

    decode_frame(frame)

    await axis_send(
        dut.clk,
        dut.mac_rx_tdata,
        dut.mac_rx_tvalid,
        dut.mac_rx_tready,
        dut.mac_rx_tlast,
        dut.mac_rx_tuser,
        scapy_arp_reply()
    )

    for _ in range(100):
        await RisingEdge(dut.clk)


# ============================================================
# TEST 1: ARP
# ============================================================

@cocotb.test()
async def test_arp_request(dut):

    cocotb.start_soon(Clock(dut.clk, 8, unit="ns").start())

    await do_reset(dut)

    dut.mac_tx_tready.value = 1
    dut.udp_rx_tready.value = 1

    recv_task = cocotb.start_soon(axis_recv(
        dut.clk,
        dut.mac_tx_tdata,
        dut.mac_tx_tvalid,
        dut.mac_tx_tready,
        dut.mac_tx_tlast
    ))

    await axis_send(
        dut.clk,
        dut.mac_rx_tdata,
        dut.mac_rx_tvalid,
        dut.mac_rx_tready,
        dut.mac_rx_tlast,
        dut.mac_rx_tuser,
        scapy_arp_request()
    )

    reply = await recv_task

    decode_frame(reply)

    assert Ether(reply).type == 0x806

    dut._log.info("PASS: ARP reply received")


# ============================================================
# TEST 2: UDP RX
# ============================================================

@cocotb.test()
async def test_udp_rx(dut):

    cocotb.start_soon(Clock(dut.clk, 8, unit="ns").start())

    await do_reset(dut)

    dut.mac_tx_tready.value = 1
    dut.udp_rx_tready.value = 1

    await arp_handshake(dut)

    payload = b"ITCH_TEST_PAYLOAD"

    frame = scapy_udp(payload)

    recv_task = cocotb.start_soon(axis_recv(
        dut.clk,
        dut.udp_rx_tdata,
        dut.udp_rx_tvalid,
        dut.udp_rx_tready,
        dut.udp_rx_tlast
    ))

    start = get_sim_time("ns")

    await axis_send(
        dut.clk,
        dut.mac_rx_tdata,
        dut.mac_rx_tvalid,
        dut.mac_rx_tready,
        dut.mac_rx_tlast,
        dut.mac_rx_tuser,
        frame
    )

    received = await recv_task

    end = get_sim_time("ns")

    print("UDP latency:", end - start, "ns")

    assert received == payload

    dut._log.info("PASS: UDP RX correct")


# ============================================================
# TEST 3: UDP BURST
# ============================================================

@cocotb.test()
async def test_udp_burst(dut):

    cocotb.start_soon(Clock(dut.clk, 8, unit="ns").start())

    await do_reset(dut)

    dut.mac_tx_tready.value = 1
    dut.udp_rx_tready.value = 1

    await arp_handshake(dut)

    for i in range(50):

        payload = f"PKT{i}".encode()

        frame = scapy_udp(payload)

        await axis_send(
            dut.clk,
            dut.mac_rx_tdata,
            dut.mac_rx_tvalid,
            dut.mac_rx_tready,
            dut.mac_rx_tlast,
            dut.mac_rx_tuser,
            frame
        )

    dut._log.info("PASS: UDP burst sent")

# ============================================================
# TEST 4: NIC
# ============================================================

@cocotb.test()
async def test_market_burst(dut):

    cocotb.start_soon(Clock(dut.clk, 8, unit="ns").start())

    await do_reset(dut)

    nic = SimulatedNIC(dut, axis_send, RisingEdge(dut.clk))

    dut.mac_tx_tready.value = 1
    dut.udp_rx_tready.value = 1

    await arp_handshake(dut)

    # simulate exchange burst
    await nic.microburst(200)

    dut._log.info("PASS: microburst traffic injected")