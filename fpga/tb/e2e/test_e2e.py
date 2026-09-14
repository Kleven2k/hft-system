"""
test_e2e.py — End-to-end FPGA pipeline tests (Phase 26)

Tests the full pipeline from raw ITCH UDP bytes in to OUCH bytes out:
  ITCH UDP (bid ADD + ask ADD) → order_book → strategy → OUCH new-order packet

Parameters match e2e_tb_wrapper.sv defaults:
  SPREAD_MAX   = 200     QUOTE_OFFSET = 2
  COOLDOWN_CYC = 10      SKEW_SHIFT   = 31 (disabled)
  FAT_FINGER_BPS = 500   MAX_BURST    = 5

Wire format: market_data_parser.sv reads RAW MAC frames, so each message is
a 42-byte ETH+IP+UDP header (see _eth_ip_udp_header) followed by the 20-byte
ITCH payload below — 62 bytes total.

ITCH payload format (20 bytes, at frame offset 42):
  [0]     msg_type  0x41='A' bid ADD  0x42='B' ask ADD
  [1..8]  timestamp (8 bytes, big-endian)
  [9..12] price     (4 bytes, big-endian, ticks)
  [13..16]shares    (4 bytes, big-endian)
  [17..18]symbol_id (2 bytes, big-endian)
  [19]    reserved  (tlast here)

OUCH 4.2 Enter Order (49 bytes per ouch_encoder.sv):
  [0]      0x4F 'O'
  [1..14]  order_token   14-byte ASCII, order_id zero-padded left
  [15]     buy_sell      0x42='B' / 0x53='S'
  [16..19] shares        uint32 big-endian
  [20..25] stock         6-byte ASCII, right-space-padded
  [26..29] price         uint32 big-endian
  [30..33] time_in_force uint32
  [34..39] firm          6-byte ASCII
  [40]     display       'Y'
  [41]     capacity      'A'/'P'
  [42]     iso_eligible  'N'
  [43..46] min_qty       uint32
  [47]     cross_type    'N'
  [48]     customer_type ' '

OUCH 4.2 Cancel Order (15 bytes): [0]=0x58 'X', [1..14]=order_token.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

CLK_NS      = 8       # 125 MHz
PORT_ITCH   = 17010
SPREAD_MAX  = 200
COOLDOWN    = 10
QUOTE_OFFSET = 2
ORDER_QTY   = 100

ORD_BUY  = 0x42   # 'B' in OUCH
ORD_SELL = 0x53   # 'S' in OUCH


# ---------------------------------------------------------------------------
# Helpers: ITCH packet sending
# ---------------------------------------------------------------------------

def _eth_ip_udp_header(dst_port=PORT_ITCH):
    """Return the 42-byte ETH+IP+UDP header market_data_parser.sv expects.

    The parser reads RAW MAC frames (not UDP payloads) and validates only
    four fields, so everything else can be zero:
      [12..13] EtherType == 0x0800 (IPv4)
      [23]     IP protocol == 0x11 (UDP)
      [36..37] UDP dest port == PORT_ITCH
    """
    hdr = [0x00] * 42
    hdr[12], hdr[13] = 0x08, 0x00          # EtherType IPv4
    hdr[23]          = 0x11                 # IP proto UDP
    hdr[36], hdr[37] = (dst_port >> 8) & 0xFF, dst_port & 0xFF
    return hdr


def _itch_packet(msg_type, price, shares, symbol_id, timestamp=0):
    """Return a 20-byte ITCH message as a list of ints (big-endian)."""
    pkt = [msg_type]
    pkt += [(timestamp >> (56 - 8*i)) & 0xFF for i in range(8)]
    pkt += [(price >> (24 - 8*i)) & 0xFF for i in range(4)]
    pkt += [(shares >> (24 - 8*i)) & 0xFF for i in range(4)]
    pkt += [(symbol_id >> 8) & 0xFF, symbol_id & 0xFF]
    pkt += [0x00]   # reserved byte — sent with tlast
    return pkt


async def send_itch(dut, msg_type, price, shares=1000, symbol_id=0):
    """Stream one raw MAC frame (header + ITCH message) on the RX AXI-Stream.

    Uses RisingEdge-THEN-assign order: wait for the edge first, then set
    the signals so they are stable for the *next* edge.  This avoids the
    Icarus/cocotb VPI ordering issue where cocotb resumes before always_ff
    and a same-delta assignment would be seen by the current clock edge.
    """
    pkt = _eth_ip_udp_header() + _itch_packet(msg_type, price, shares, symbol_id)
    for i, byte in enumerate(pkt):
        await RisingEdge(dut.rxc_clk)  # sync to edge first
        dut.rx_tdata.value    = byte
        dut.rx_tvalid.value   = 1
        dut.rx_tlast.value    = 1 if i == len(pkt) - 1 else 0
        dut.rx_dst_port.value = PORT_ITCH
    await RisingEdge(dut.rxc_clk)      # let the last byte be sampled
    dut.rx_tvalid.value = 0
    dut.rx_tlast.value  = 0


# ---------------------------------------------------------------------------
# Helpers: OUCH capture
# ---------------------------------------------------------------------------

async def capture_ouch(dut, timeout=900):
    """
    Capture one complete OUCH packet from the UDP TX stream (rxc_clk domain).
    Returns the byte list on success, None on timeout.
    Only captures packets starting with 0x4F (new order) or 0x58 (cancel).
    Telemetry packets (0x48 magic) are skipped.
    """
    buf = []
    skipping = False

    for _ in range(timeout):
        await RisingEdge(dut.rxc_clk)
        if dut.tx_tvalid.value and dut.tx_tready.value:
            byte = int(dut.tx_tdata.value)
            if not buf:
                # First byte: check if this is an OUCH packet
                if byte in (0x4F, 0x58):
                    buf.append(byte)
                    skipping = False
                else:
                    # Telemetry or other — skip until tlast
                    skipping = True
            elif not skipping:
                buf.append(byte)

            if dut.tx_tlast.value:
                if skipping:
                    skipping = False
                    buf = []
                elif buf:
                    return buf
    return None


OUCH_ENTER_LEN  = 49
OUCH_CANCEL_LEN = 15


def token_to_order_id(token: str) -> int:
    """Decode ouch_encoder.sv's 14-char ASCII hex order token.

    make_token() writes ASCII char i at bit position i*8, so char 0 lands in
    the LOW byte — but the shift register transmits MSB-first, which puts the
    LAST-written char on the wire first. The on-wire digits are therefore the
    reverse of the hex representation: order_id=1 transmits "10000000000000",
    not "00000000000001". (Same nibble-order trap that bit soup_session's
    token_to_id during Phase 28 bring-up.)
    """
    return int(token[::-1], 16)


def decode_ouch_new_order(pkt):
    """Parse a 49-byte OUCH 4.2 Enter Order packet into a dict."""
    assert pkt[0] == 0x4F, f"Expected 0x4F, got 0x{pkt[0]:02X}"
    assert len(pkt) == OUCH_ENTER_LEN, f"Expected {OUCH_ENTER_LEN} bytes, got {len(pkt)}"
    token = bytes(pkt[1:15]).decode("ascii")
    return {
        "msg_type": pkt[0],
        "token":    token,
        "order_id": token_to_order_id(token),
        "side":     pkt[15],
        "qty":      int.from_bytes(bytes(pkt[16:20]), "big"),
        "stock":    bytes(pkt[20:26]).decode("ascii").rstrip(),
        "price":    int.from_bytes(bytes(pkt[26:30]), "big"),
    }


def decode_ouch_cancel(pkt):
    """Parse a 15-byte OUCH 4.2 Cancel Order packet into a dict."""
    assert pkt[0] == 0x58, f"Expected 0x58, got 0x{pkt[0]:02X}"
    token = bytes(pkt[1:15]).decode("ascii")
    return {"msg_type": pkt[0], "token": token, "order_id": token_to_order_id(token)}


# ---------------------------------------------------------------------------
# Shared reset helper
# ---------------------------------------------------------------------------

async def reset_dut(dut):
    dut.rst.value         = 1
    dut.rxc_rst_n.value   = 0
    dut.rx_tvalid.value   = 0
    dut.rx_tlast.value    = 0
    dut.rx_tdata.value    = 0
    dut.rx_dst_port.value = 0
    dut.tx_tready.value   = 1   # always ready to accept TX bytes
    dut.kill_switch.value = 0
    dut.ack_raw_valid.value    = 0
    dut.ack_raw_order_id.value = 0
    dut.ack_raw_status.value   = 0
    dut.ack_raw_fill_qty.value = 0

    await ClockCycles(dut.clk, 8)
    dut.rst.value       = 0
    dut.rxc_rst_n.value = 1
    await ClockCycles(dut.clk, 300)  # 256 for order_book init_done + CDC + strategy settle


# ---------------------------------------------------------------------------
# Test 1: basic end-to-end — ITCH quotes → OUCH new order
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_bid_ask_generates_ouch_order(dut):
    """
    Send ITCH bid ADD + ask ADD for slot 0 with tight spread.
    Expect OUCH new-order packet on TX with correct symbol, side, and price.

    bid = 100, ask = 110, spread = 10 ≤ SPREAD_MAX=200 → strategy fires.
    BUY at bid − QUOTE_OFFSET = 100 − 2 = 98.
    """
    cocotb.start_soon(Clock(dut.clk,    CLK_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rxc_clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid, ask = 100, 110

    await send_itch(dut, 0x41, bid, shares=500, symbol_id=0)
    await ClockCycles(dut.rxc_clk, 2)
    await send_itch(dut, 0x42, ask, shares=500, symbol_id=0)

    pkt = await capture_ouch(dut, timeout=800)
    assert pkt is not None, "No OUCH packet received within timeout"
    order = decode_ouch_new_order(pkt)
    assert order["stock"]     == "AAPL", f"stock={order['stock']!r}"
    assert order["side"]      == ORD_BUY, f"Expected BUY (0x42), got 0x{order['side']:02X}"
    assert order["price"]     == bid - QUOTE_OFFSET, \
        f"price={order['price']}, expected {bid - QUOTE_OFFSET}"
    assert order["qty"]       == ORDER_QTY, f"qty={order['qty']}"


# ---------------------------------------------------------------------------
# Test 2: wide spread → no order within timeout
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_wide_spread_no_order(dut):
    """
    Spread = 300 > SPREAD_MAX=200 → strategy must NOT fire.
    Send bid=100, ask=400.  No OUCH packet expected.
    """
    cocotb.start_soon(Clock(dut.clk,    CLK_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rxc_clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    await send_itch(dut, 0x41, 100, symbol_id=0)
    await ClockCycles(dut.rxc_clk, 2)
    await send_itch(dut, 0x42, 400, symbol_id=0)

    pkt = await capture_ouch(dut, timeout=600)
    assert pkt is None, f"Got unexpected OUCH packet: {pkt}"


# ---------------------------------------------------------------------------
# Test 3: correct symbol routing — slot 2
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_symbol_routing_slot2(dut):
    """
    Send ITCH quotes with symbol_id=2.  OUCH packet must show symbol_id=2.
    """
    cocotb.start_soon(Clock(dut.clk,    CLK_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rxc_clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid, ask = 100, 108   # spread=8 ≤ SPREAD_MAX=200

    await send_itch(dut, 0x41, bid, symbol_id=2)
    await ClockCycles(dut.rxc_clk, 2)
    await send_itch(dut, 0x42, ask, symbol_id=2)

    pkt = await capture_ouch(dut, timeout=800)
    assert pkt is not None, "No OUCH packet for slot 2"
    order = decode_ouch_new_order(pkt)
    assert order["stock"] == "AMZN", f"Expected slot 2 = AMZN, got {order['stock']!r}"


# ---------------------------------------------------------------------------
# Test 4: OUCH packet integrity — all bytes correct
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_ouch_packet_bytes(dut):
    """
    Full byte-level verification of the OUCH new-order packet.
      byte[0]    = 0x4F  ('O')
      byte[1..2] = symbol_id = 0  (big-endian)
      byte[3]    = 0x42  ('B' = BUY)
      byte[4..7] = price = bid − QUOTE_OFFSET  (big-endian)
      byte[8..11]= qty   = ORDER_QTY           (big-endian)
      byte[12..19] = order_id (big-endian, non-zero)
    """
    cocotb.start_soon(Clock(dut.clk,    CLK_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rxc_clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid, ask = 200, 208

    await send_itch(dut, 0x41, bid, symbol_id=0)
    await ClockCycles(dut.rxc_clk, 2)
    await send_itch(dut, 0x42, ask, symbol_id=0)

    pkt = await capture_ouch(dut, timeout=800)
    assert pkt is not None, "No OUCH packet received"
    assert len(pkt) == OUCH_ENTER_LEN, f"Expected {OUCH_ENTER_LEN} bytes, got {len(pkt)}"

    expected_price = bid - QUOTE_OFFSET
    assert pkt[0] == 0x4F, f"byte[0] (msg type) = 0x{pkt[0]:02X}"

    # [1..14] order token — 14 ASCII hex digits, non-zero
    token = bytes(pkt[1:15]).decode("ascii")
    assert all(c in "0123456789ABCDEF" for c in token), f"bad token {token!r}"
    assert int(token, 16) > 0, "order_id must be non-zero"

    assert pkt[15] == ORD_BUY, f"byte[15] (side) = 0x{pkt[15]:02X}"
    assert int.from_bytes(bytes(pkt[16:20]), "big") == ORDER_QTY, \
        f"shares = {int.from_bytes(bytes(pkt[16:20]), 'big')}"
    assert bytes(pkt[20:26]) == b"AAPL  ", f"stock = {bytes(pkt[20:26])!r}"
    assert int.from_bytes(bytes(pkt[26:30]), "big") == expected_price, \
        f"price = {int.from_bytes(bytes(pkt[26:30]), 'big')}, expected {expected_price}"

    # Trailing fixed fields
    assert pkt[40] == ord("Y"), f"byte[40] (display) = 0x{pkt[40]:02X}"
    assert pkt[42] == ord("N"), f"byte[42] (iso_eligible) = 0x{pkt[42]:02X}"
    assert pkt[47] == ord("N"), f"byte[47] (cross_type) = 0x{pkt[47]:02X}"
    assert pkt[48] == ord(" "), f"byte[48] (customer_type) = 0x{pkt[48]:02X}"


# ---------------------------------------------------------------------------
# Test 5: kill switch blocks order emission
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_kill_switch_blocks_ouch(dut):
    """kill_switch=1 prevents any OUCH packet being emitted."""
    cocotb.start_soon(Clock(dut.clk,    CLK_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rxc_clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    dut.kill_switch.value = 1

    await send_itch(dut, 0x41, 100, symbol_id=0)
    await ClockCycles(dut.rxc_clk, 2)
    await send_itch(dut, 0x42, 110, symbol_id=0)

    pkt = await capture_ouch(dut, timeout=600)
    assert pkt is None, f"kill_switch should block order, got: {pkt}"


# ---------------------------------------------------------------------------
# Test 6: order ID increments on successive orders (with ACK)
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_order_id_increments(dut):
    """
    After ACK_FILLED clears pending + cooldown expires, a second order fires
    with order_id = first_id + 1.
    """
    ACK_FILLED = 0x00

    cocotb.start_soon(Clock(dut.clk,    CLK_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rxc_clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid, ask = 100, 108

    # ---- First order ----
    await send_itch(dut, 0x41, bid, symbol_id=0)
    await ClockCycles(dut.rxc_clk, 2)
    await send_itch(dut, 0x42, ask, symbol_id=0)

    pkt1 = await capture_ouch(dut, timeout=800)
    assert pkt1 is not None, "First order never arrived"
    order1 = decode_ouch_new_order(pkt1)

    # ---- ACK the first order (drive in rxc_clk domain) ----
    # Use await-first pattern: signals set after RisingEdge are seen by always_ff
    # at the SAME edge (VPI fires before always_ff in Icarus).
    await RisingEdge(dut.rxc_clk)
    dut.ack_raw_valid.value    = 1
    dut.ack_raw_order_id.value = order1["order_id"]
    dut.ack_raw_status.value   = ACK_FILLED
    dut.ack_raw_fill_qty.value = ORDER_QTY
    await RisingEdge(dut.rxc_clk)
    dut.ack_raw_valid.value = 0

    # ---- Capture second order immediately (no wait) ----
    # The second order fires as soon as pending clears (ACK CDC ~3 cycles) and
    # cooldown expires (COOLDOWN_CYC cycles from first-order fire, may have already
    # elapsed).  Start capture_ouch now so the OUCH bytes don't scroll past us.
    pkt2 = await capture_ouch(dut, timeout=COOLDOWN + 900)
    assert pkt2 is not None, "Second order never arrived"
    order2 = decode_ouch_new_order(pkt2)

    assert order2["order_id"] == order1["order_id"] + 1, \
        f"order_id did not increment: {order1['order_id']} → {order2['order_id']}"
