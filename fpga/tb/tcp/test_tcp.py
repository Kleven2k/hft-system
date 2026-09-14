"""
test_tcp.py — Phase 27 cocotb tests for tcp_engine + soup_session.

Tests the full TCP order path:
  1. test_arp_request    — DUT broadcasts ARP who-has SERVER_IP; we reply.
  2. test_tcp_handshake  — SYN → SYN-ACK → ACK; 'connected' goes high.
  3. test_soup_login     — Login Request sent; Login Accepted replied; 'session_active' high.
  4. test_order_submit   — OUCH Enter Order drives 'U' SoupBinTCP frame on MAC TX.
  5. test_exec_report    — Accepted report received; ack_valid fires with correct order_id.
  6. test_heartbeat      — After HB_CYC cycles, 'R' heartbeat sent.

'Exchange peer' parameters (match tcp_engine defaults):
  LOCAL_MAC  = 02:00:00:00:00:01   (DUT)
  LOCAL_IP   = 192.168.1.10
  SERVER_IP  = 192.168.1.11        (DUT ARPs for this — direct connection, no gateway)
  SERVER_IP  = 192.168.1.11        (us — TCP peer)
  LOCAL_PORT = 4201
  SERVER_PORT= 4200
  INIT_SEQ   = 0xA5C37B21          (DUT ISN)
  SERVER_MAC = 02:00:00:00:00:02   (our MAC, returned in ARP reply)
  SERVER_ISN = 0x12345678          (our TCP ISN)
"""

import struct
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, with_timeout

# ── fixed parameters matching tcp_engine defaults ──────────────────────────
LOCAL_MAC   = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01])
LOCAL_IP    = bytes([0xC0, 0xA8, 0x01, 0x0A])  # 192.168.1.10
GATEWAY_IP  = bytes([0xC0, 0xA8, 0x01, 0x01])  # 192.168.1.1
SERVER_IP   = bytes([0xC0, 0xA8, 0x01, 0x0B])  # 192.168.1.11
LOCAL_PORT  = 4201
SERVER_PORT = 4200
INIT_SEQ    = 0xA5C37B21   # DUT's ISN

SERVER_MAC  = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x02])  # our (test) MAC
SERVER_ISN  = 0x12345678   # our (test) TCP ISN

HB_CYC      = 500   # matches wrapper parameter


# ── checksum helpers ────────────────────────────────────────────────────────

def ones_complement(data: bytes) -> int:
    s = 0
    for i in range(0, len(data) - 1, 2):
        s += (data[i] << 8) | data[i + 1]
    if len(data) & 1:
        s += data[-1] << 8
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def ip_checksum(hdr: bytes) -> int:
    """Compute IP header checksum (checksum field must be zero)."""
    return ones_complement(hdr)


def tcp_checksum(src_ip: bytes, dst_ip: bytes, tcp_seg: bytes) -> int:
    pseudo = src_ip + dst_ip + bytes([0, 6]) + struct.pack('!H', len(tcp_seg))
    return ones_complement(pseudo + tcp_seg)


# ── frame builders ──────────────────────────────────────────────────────────

def build_arp_reply(sender_mac: bytes, sender_ip: bytes,
                    target_mac: bytes, target_ip: bytes) -> bytes:
    """Build a 60-byte ARP reply frame."""
    eth  = target_mac + sender_mac + bytes([0x08, 0x06])
    arp  = struct.pack('!HHBBH', 1, 0x0800, 6, 4, 2)  # htype, ptype, hlen, plen, op=reply
    arp += sender_mac + sender_ip + target_mac + target_ip
    frame = eth + arp
    return frame + bytes(60 - len(frame))  # pad to 60


def build_syn_ack(our_seq: int, ack_num: int) -> bytes:
    """Build a TCP SYN-ACK frame addressed to the DUT."""
    # IP header (20 bytes, checksum=0 first)
    ip_total = 40  # 20 IP + 20 TCP
    ip_hdr = struct.pack('!BBHHHBBH4s4s',
        0x45, 0,         # version/IHL, DSCP/ECN
        ip_total,        # total length
        0x1234,          # identification
        0x4000,          # flags (DF) + fragment offset
        64,              # TTL
        6,               # protocol = TCP
        0,               # checksum placeholder
        SERVER_IP,
        LOCAL_IP
    )
    ip_chk = ip_checksum(ip_hdr)
    ip_hdr = ip_hdr[:10] + struct.pack('!H', ip_chk) + ip_hdr[12:]

    # TCP header (20 bytes):
    # [12]=data_offset(0x50) and [13]=flags(0x12) packed as one H=0x5012
    tcp_seg = struct.pack('!HHIIHHHH',
        SERVER_PORT, LOCAL_PORT,
        our_seq, ack_num,
        0x5012,   # byte12=0x50 (data_offset=5), byte13=0x12 (SYN+ACK)
        0xFFFF,   # window
        0,        # checksum placeholder
        0,        # urgent
    )
    tcp_chk = tcp_checksum(SERVER_IP, LOCAL_IP, tcp_seg)
    tcp_seg = tcp_seg[:16] + struct.pack('!H', tcp_chk) + tcp_seg[18:]

    eth = LOCAL_MAC + SERVER_MAC + bytes([0x08, 0x00])
    return eth + ip_hdr + tcp_seg


def build_tcp_ack(our_seq: int, ack_num: int, payload: bytes = b'') -> bytes:
    """Build a TCP ACK (or PSH+ACK) frame to the DUT."""
    flags_byte = 0x18 if payload else 0x10  # PSH+ACK or pure ACK
    ip_total = 40 + len(payload)
    ip_hdr = struct.pack('!BBHHHBBH4s4s',
        0x45, 0, ip_total, 0x1235, 0x4000, 64, 6, 0, SERVER_IP, LOCAL_IP)
    ip_chk = ip_checksum(ip_hdr)
    ip_hdr = ip_hdr[:10] + struct.pack('!H', ip_chk) + ip_hdr[12:]

    # byte12=0x50 (data_offset=5), byte13=flags — merged into H
    tcp_hdr = struct.pack('!HHIIHHHH',
        SERVER_PORT, LOCAL_PORT,
        our_seq, ack_num,
        (0x5000 | flags_byte),   # byte12=0x50, byte13=flags
        0xFFFF,                   # window
        0,                        # checksum placeholder
        0,                        # urgent
    )
    tcp_seg = tcp_hdr + payload
    tcp_chk = tcp_checksum(SERVER_IP, LOCAL_IP, tcp_seg)
    tcp_seg = tcp_seg[:16] + struct.pack('!H', tcp_chk) + tcp_seg[18:]

    eth = LOCAL_MAC + SERVER_MAC + bytes([0x08, 0x00])
    return eth + ip_hdr + tcp_seg


def build_soup_frame(msg_type: int, payload: bytes) -> bytes:
    """Wrap payload in SoupBinTCP framing: [len_hi][len_lo][type][payload]."""
    length = 1 + len(payload)
    return struct.pack('!HB', length, msg_type) + payload


def build_login_accepted() -> bytes:
    """SoupBinTCP Login Accepted: session(10) + seq_num(20 ASCII)."""
    session  = b'TESTSESS01'            # 10 bytes
    seq_num  = b'                   1'  # 20 bytes (19 spaces + '1')
    return build_soup_frame(0x41, session + seq_num)


def build_sequenced_data(ouch_payload: bytes) -> bytes:
    """SoupBinTCP Sequenced Data ('S') wrapping an OUCH execution report."""
    return build_soup_frame(0x53, ouch_payload)


def build_ouch_accepted(order_token: bytes) -> bytes:
    """
    OUCH Accepted message (type 0x41 'A'), minimal fields.
    Payload structure for inbound Accepted:
      [0]     0x41 'A'
      [1-14]  order_token (14 bytes)
      [15]    buy_sell
      [16-19] shares uint32
      [20-25] stock  6 bytes
      [26-29] price  uint32
      [30-33] TIF    uint32
      [34-39] firm   6 bytes
      [40]    display
      [41]    capacity
      [42]    ISO
      [43-46] min_qty uint32
      [47]    cross_type
      [48]    customer_type
      [49-52] order_ref uint32 (exchange-assigned)
    Total: 53 bytes
    """
    assert len(order_token) == 14
    return (bytes([0x41]) +          # 'A' Accepted
            order_token +            # [1-14]
            bytes([0x42]) +          # Buy
            struct.pack('!I', 100) + # shares = 100
            b'AAPL  ' +              # stock
            struct.pack('!I', 1500000) +  # price
            struct.pack('!I', 99998) +    # TIF
            b'FIRM  ' +              # firm
            bytes([0x59, 0x41, 0x4E]) +  # display, capacity, ISO
            struct.pack('!I', 0) +   # min_qty
            bytes([0x4E, 0x20]) +    # cross_type, customer_type
            struct.pack('!I', 42))   # order_ref


def make_order_token(order_id: int) -> bytes:
    """14-char upper-case hex ASCII token, in wire order.

    ouch_encoder.sv's make_token() writes ASCII char i at bit i*8 while the
    shift register transmits MSB-first, so the on-wire digits are the REVERSE
    of the hex representation: order_id=1 goes out as "10000000000000", not
    "00000000000001". soup_session's token_to_id decodes with that same
    reversal, so a token built forward here round-trips to a nibble-swapped
    order_id (0xDEADBEEF came back as 0xFEEBDAED000000).
    """
    return f'{order_id & 0xFFFFFFFFFFFFFF:014X}'[::-1].encode('ascii')


# ── AXI-Stream helpers ──────────────────────────────────────────────────────

async def send_mac_frame(dut, data: bytes):
    """Drive mac_rx_t* with a frame, byte by byte."""
    for i, byte in enumerate(data):
        dut.mac_rx_tdata.value  = byte
        dut.mac_rx_tvalid.value = 1
        dut.mac_rx_tlast.value  = 1 if i == len(data) - 1 else 0
        dut.mac_rx_tuser.value  = 0
        await RisingEdge(dut.clk)
    dut.mac_rx_tvalid.value = 0
    dut.mac_rx_tlast.value  = 0


async def recv_mac_frame(dut, timeout_cycles: int = 2000) -> bytes:
    """Capture one complete MAC TX frame (until tlast)."""
    dut.mac_tx_tready.value = 1
    data = []
    for _ in range(timeout_cycles):
        await RisingEdge(dut.clk)
        if dut.mac_tx_tvalid.value and dut.mac_tx_tready.value:
            data.append(int(dut.mac_tx_tdata.value))
            if dut.mac_tx_tlast.value:
                return bytes(data)
    raise TimeoutError(f"recv_mac_frame: no tlast after {timeout_cycles} cycles")


def tcp_payload_of(frame: bytes) -> bytes:
    """Extract the TCP payload, honouring the real IHL and data-offset fields
    instead of assuming a fixed 54-byte header."""
    ihl  = (frame[14] & 0x0F) * 4
    off  = 14 + ihl
    doff = ((frame[off + 12] >> 4) & 0x0F) * 4
    return frame[off + doff:]


async def recv_tcp_data_frame(dut, timeout_cycles: int = 3000) -> bytes:
    """Capture the next TX frame that actually carries TCP payload.

    tcp_engine sends a bare ACK to complete the three-way handshake before
    soup_session's Login Request goes out, so a test that grabs the very next
    frame after the handshake gets a 0-byte ACK, not the Login. Skip pure ACKs
    (no payload) and return the first data-bearing frame.
    """
    deadline = timeout_cycles
    while deadline > 0:
        frame = await recv_mac_frame(dut, timeout_cycles=deadline)
        payload = tcp_payload_of(frame)
        if payload:
            return frame
        deadline -= 200   # rough cost of the skipped frame
    raise TimeoutError("recv_tcp_data_frame: only pure ACKs seen")


async def send_ouch_enter(dut, order_id: int = 1):
    """Drive a minimal OUCH Enter Order frame onto ouch_t* ports."""
    token  = make_order_token(order_id)
    frame  = (bytes([0x4F]) +       # 'O' Enter
              token +               # [1-14]
              bytes([0x42]) +       # Buy
              struct.pack('!I', 100) +      # shares
              b'AAPL  ' +           # stock
              struct.pack('!I', 1500000) +  # price
              struct.pack('!I', 99998) +    # TIF
              b'FIRM  ' +           # firm
              bytes([0x59, 0x41, 0x4E]) +   # display, capacity, ISO
              struct.pack('!I', 0) +        # min_qty
              bytes([0x4E, 0x20]))           # cross_type, customer_type
    assert len(frame) == 49

    for i, byte in enumerate(frame):
        dut.ouch_tdata.value  = byte
        dut.ouch_tvalid.value = 1
        dut.ouch_tlast.value  = 1 if i == len(frame) - 1 else 0
        # Wait for tready before advancing
        for _ in range(100):
            await RisingEdge(dut.clk)
            if dut.ouch_tready.value:
                break
    dut.ouch_tvalid.value = 0
    dut.ouch_tlast.value  = 0


# ── common setup ────────────────────────────────────────────────────────────

async def reset_dut(dut):
    dut.rst_n.value         = 0
    dut.mac_rx_tvalid.value = 0
    dut.mac_rx_tlast.value  = 0
    dut.mac_rx_tuser.value  = 0
    dut.mac_rx_tdata.value  = 0
    dut.mac_tx_tready.value = 1
    dut.ouch_tvalid.value   = 0
    dut.ouch_tlast.value    = 0
    dut.ouch_tdata.value    = 0
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def do_arp_exchange(dut) -> bytes:
    """Wait for ARP request, reply, return gateway MAC (= SERVER_MAC)."""
    arp_frame = await recv_mac_frame(dut, timeout_cycles=500)
    # Verify ARP ethertype
    assert arp_frame[12:14] == bytes([0x08, 0x06]), \
        f"Expected ARP (0x0806), got {arp_frame[12:14].hex()}"
    assert arp_frame[20:22] == bytes([0x00, 0x01]), "Expected ARP request (op=1)"
    # Target IP should be SERVER_IP (ARP directly for server, no gateway needed)
    assert arp_frame[38:42] == SERVER_IP, \
        f"ARP target IP mismatch: {arp_frame[38:42].hex()}"
    # Reply: SERVER_MAC claims SERVER_IP
    reply = build_arp_reply(
        sender_mac=SERVER_MAC, sender_ip=SERVER_IP,
        target_mac=LOCAL_MAC,  target_ip=LOCAL_IP
    )
    await send_mac_frame(dut, reply)
    return SERVER_MAC


async def do_tcp_handshake(dut) -> int:
    """
    Complete TCP 3-way handshake. Returns server_seq_after_handshake (ISN+1).
    Assumes ARP already done.
    """
    # Receive SYN
    syn_frame = await recv_mac_frame(dut, timeout_cycles=500)
    assert syn_frame[23] == 6, "Expected TCP (proto=6)"
    tcp_flags_byte = syn_frame[47]
    assert tcp_flags_byte == 0x02, \
        f"Expected SYN (0x02), got 0x{tcp_flags_byte:02X}"
    dut_isn = struct.unpack('!I', syn_frame[38:42])[0]
    assert dut_isn == INIT_SEQ, f"DUT ISN mismatch: got 0x{dut_isn:08X}"

    # Send SYN-ACK
    syn_ack = build_syn_ack(our_seq=SERVER_ISN, ack_num=INIT_SEQ + 1)
    await send_mac_frame(dut, syn_ack)

    # Receive ACK (completes handshake)
    ack_frame = await recv_mac_frame(dut, timeout_cycles=500)
    ack_flags = ack_frame[47]
    assert ack_flags == 0x10, f"Expected ACK (0x10), got 0x{ack_flags:02X}"

    # Verify 'connected' is high
    for _ in range(20):
        await RisingEdge(dut.clk)
        if dut.connected.value:
            break
    assert dut.connected.value, "'connected' did not go high after handshake"

    return SERVER_ISN + 1  # next expected sequence from server side


async def do_soup_login(dut, server_seq: int) -> int:
    """
    Receive Login Request, send Login Accepted, wait for session_active.
    Returns updated server_seq.
    """
    # Receive TCP frame carrying Login Request ('L')
    login_frame = await recv_tcp_data_frame(dut, timeout_cycles=3000)
    tcp_payload = tcp_payload_of(login_frame)
    assert len(tcp_payload) >= 3, f"TCP payload too short: {len(tcp_payload)} bytes"
    # SoupBinTCP: [len_hi][len_lo][type][payload]
    soup_len  = struct.unpack('!H', tcp_payload[0:2])[0]
    soup_type = tcp_payload[2]
    assert soup_type == 0x4C, f"Expected Login 'L' (0x4C), got 0x{soup_type:02X}"
    login_payload = tcp_payload[3:]
    assert login_payload[0:6] == b'USER  ', \
        f"Username mismatch: {login_payload[0:6]}"

    # Build Login Accepted and wrap in TCP
    login_acc   = build_login_accepted()  # SoupBinTCP 'A' frame
    dut_seq_ack = struct.unpack('!I', login_frame[42:46])[0] + 1  # ack their seq

    tcp_data_frame = build_tcp_ack(
        our_seq=server_seq,
        ack_num=INIT_SEQ + 1,
        payload=login_acc
    )
    await send_mac_frame(dut, tcp_data_frame)

    # Wait for session_active
    for _ in range(300):
        await RisingEdge(dut.clk)
        if dut.session_active.value:
            break
    assert dut.session_active.value, "'session_active' did not go high after Login Accepted"

    # Consume the TCP ACK that the DUT sends in response to our Login Accepted data frame.
    # This ACK is sent immediately after mac_rx_tlast fires for the Login Accepted.
    ack_frame = await recv_mac_frame(dut, timeout_cycles=200)
    # Pure ACK has no TCP payload (frame len = 14 ETH + 20 IP + 20 TCP = 54 bytes)
    assert len(ack_frame) == 54, \
        f"Expected pure ACK (54 bytes) after Login Accepted, got {len(ack_frame)}"

    return server_seq + len(login_acc)


# ── test cases ───────────────────────────────────────────────────────────────

@cocotb.test()
async def test_arp_request(dut):
    """DUT sends ARP who-has SERVER_IP after reset."""
    cocotb.start_soon(Clock(dut.clk, 8, units='ns').start())
    await reset_dut(dut)
    gw_mac = await do_arp_exchange(dut)
    assert gw_mac == SERVER_MAC, "Gateway MAC not as expected"
    dut._log.info("PASS: ARP request/reply")


@cocotb.test()
async def test_tcp_handshake(dut):
    """ARP + SYN → SYN-ACK → ACK; 'connected' goes high."""
    cocotb.start_soon(Clock(dut.clk, 8, units='ns').start())
    await reset_dut(dut)
    await do_arp_exchange(dut)
    await do_tcp_handshake(dut)
    dut._log.info("PASS: TCP 3-way handshake, connected=1")


@cocotb.test()
async def test_soup_login(dut):
    """Login Request → Login Accepted → session_active high."""
    cocotb.start_soon(Clock(dut.clk, 8, units='ns').start())
    await reset_dut(dut)
    await do_arp_exchange(dut)
    server_seq = await do_tcp_handshake(dut)
    await do_soup_login(dut, server_seq)
    dut._log.info("PASS: SoupBinTCP login, session_active=1")


@cocotb.test()
async def test_order_submit(dut):
    """OUCH Enter Order → SoupBinTCP 'U' frame on MAC TX."""
    cocotb.start_soon(Clock(dut.clk, 8, units='ns').start())
    await reset_dut(dut)
    await do_arp_exchange(dut)
    server_seq = await do_tcp_handshake(dut)
    await do_soup_login(dut, server_seq)

    # Drive OUCH Enter Order (order_id=1)
    order_id = 1
    cocotb.start_soon(send_ouch_enter(dut, order_id))

    # Capture the resulting TCP frame
    frame = await recv_tcp_data_frame(dut, timeout_cycles=3000)
    tcp_payload = tcp_payload_of(frame)

    # SoupBinTCP header
    soup_len  = struct.unpack('!H', tcp_payload[0:2])[0]
    soup_type = tcp_payload[2]
    assert soup_type == 0x55, \
        f"Expected SoupBinTCP 'U' (0x55), got 0x{soup_type:02X}"
    assert soup_len == 50, \
        f"Expected soup_len=50 for Enter Order, got {soup_len}"

    # OUCH payload starts at tcp_payload[3]
    ouch = tcp_payload[3:]
    assert ouch[0] == 0x4F, f"Expected OUCH Enter 'O' (0x4F), got 0x{ouch[0]:02X}"
    token_in_frame = ouch[1:15]
    expected_token = make_order_token(order_id)
    assert token_in_frame == expected_token, \
        f"Token mismatch: {token_in_frame} != {expected_token}"

    dut._log.info(f"PASS: OUCH Enter Order in SoupBinTCP 'U' frame, token={token_in_frame.decode()}")


@cocotb.test()
async def test_exec_report(dut):
    """Server sends Accepted exec report → ack_valid fires with correct order_id."""
    cocotb.start_soon(Clock(dut.clk, 8, units='ns').start())
    await reset_dut(dut)
    await do_arp_exchange(dut)
    server_seq = await do_tcp_handshake(dut)
    server_seq = await do_soup_login(dut, server_seq)

    # Drive an order so DUT has something to ACK
    order_id = 0xDEADBEEF
    token    = make_order_token(order_id)

    # Build OUCH Accepted report in SoupBinTCP 'S' frame
    ouch_acc    = build_ouch_accepted(token)
    soup_s      = build_sequenced_data(ouch_acc)
    tcp_data    = build_tcp_ack(our_seq=server_seq, ack_num=INIT_SEQ + 1, payload=soup_s)
    await send_mac_frame(dut, tcp_data)

    # Wait for ack_valid pulse
    got_ack = False
    for _ in range(300):
        await RisingEdge(dut.clk)
        if dut.ack_valid.value:
            got_ack = True
            break
    assert got_ack, "ack_valid never fired"

    recv_id     = int(dut.ack_order_id.value)
    recv_status = int(dut.ack_status.value)
    assert recv_id == order_id, \
        f"ack_order_id mismatch: got 0x{recv_id:X}, expected 0x{order_id:X}"
    assert recv_status == 0x01, \
        f"Expected ACK_PARTIAL (0x01) for Accepted, got 0x{recv_status:02X}"

    dut._log.info(f"PASS: exec report → ack_valid, order_id=0x{recv_id:X}, status=0x{recv_status:02X}")


@cocotb.test()
async def test_heartbeat(dut):
    """After HB_CYC cycles in ACTIVE state, SoupBinTCP 'R' heartbeat is sent."""
    cocotb.start_soon(Clock(dut.clk, 8, units='ns').start())
    await reset_dut(dut)
    await do_arp_exchange(dut)
    server_seq = await do_tcp_handshake(dut)
    await do_soup_login(dut, server_seq)

    # Wait for heartbeat: soup_session fires after HB_CYC cycles in ACTIVE state.
    # recv_mac_frame polls continuously, so it captures the frame as it arrives.
    frame = await recv_tcp_data_frame(dut, timeout_cycles=HB_CYC + 2000)
    tcp_payload = tcp_payload_of(frame)
    soup_len  = struct.unpack('!H', tcp_payload[0:2])[0]
    soup_type = tcp_payload[2]
    assert soup_type == 0x52, \
        f"Expected heartbeat 'R' (0x52), got 0x{soup_type:02X}"
    assert soup_len == 1, f"Heartbeat soup_len should be 1, got {soup_len}"

    dut._log.info("PASS: SoupBinTCP 'R' heartbeat sent after HB_CYC cycles")
