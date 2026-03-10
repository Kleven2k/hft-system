"""
test_ouch_encoder.py
Verifies ouch_encoder.sv: byte layout, backpressure, consecutive
orders, and sideband stability.

OUCH message format (20 bytes):
  Byte  0   : 0x4F ('O')
  Bytes 1-2 : symbol_id BE
  Byte  3   : side 0x42='B' / 0x53='S'
  Bytes 4-7 : price BE
  Bytes 8-11: quantity BE
  Bytes 12-19: order_id BE
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

CLK_NS   = 8
PORT_OUCH = 42000
MSG_LEN   = 20
ORD_BUY  = 0
ORD_SELL = 1


def _int_to_bytes_be(val, n):
    return [(val >> (8 * (n - 1 - i))) & 0xFF for i in range(n)]


def build_expected(order):
    side_byte = 0x42 if order["side"] == ORD_BUY else 0x53
    return (
        [0x4F]
        + _int_to_bytes_be(order["symbol_id"], 2)
        + [side_byte]
        + _int_to_bytes_be(order["price"], 4)
        + _int_to_bytes_be(order["quantity"], 4)
        + _int_to_bytes_be(order["order_id"], 8)
    )


async def reset_dut(dut):
    dut.rst.value         = 1
    dut.order_valid.value = 0
    dut.tx_tready.value   = 1
    _drive_order(dut, {"order_id": 0, "price": 0, "quantity": 0,
                        "symbol_id": 0, "side": ORD_BUY})
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)


def _drive_order(dut, order):
    dut.order_in_order_id.value  = order["order_id"]
    dut.order_in_price.value     = order["price"]
    dut.order_in_quantity.value  = order["quantity"]
    dut.order_in_symbol_id.value = order["symbol_id"]
    dut.order_in_side.value      = order["side"]
    dut.order_in_ord_type.value  = 1   # ORD_LIMIT
    dut.order_in_valid.value     = 1


async def send_order(dut, order):
    await RisingEdge(dut.clk)
    _drive_order(dut, order)
    dut.order_valid.value = 1
    await RisingEdge(dut.clk)
    dut.order_valid.value = 0
    dut.order_in_valid.value = 0


async def collect_bytes(dut, count, timeout=60):
    """Collect `count` AXI-S bytes, respecting tready. Returns list of ints."""
    received = []
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        if dut.tx_tvalid.value == 1 and dut.tx_tready.value == 1:
            received.append(int(dut.tx_tdata.value))
            if len(received) == count:
                return received
    return received


# ── Tests ──────────────────────────────────────────────────────

@cocotb.test()
async def test_byte_layout(dut):
    """20-byte OUCH message matches expected field encoding."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    order = {
        "order_id": 0xDEADBEEF_CAFEBABE,
        "price":    1_234_567,
        "quantity": 100,
        "symbol_id": 3,
        "side":     ORD_BUY,
    }

    await send_order(dut, order)
    received = await collect_bytes(dut, MSG_LEN, timeout=60)

    assert len(received) == MSG_LEN, f"only got {len(received)} bytes"
    expected = build_expected(order)
    for i, (got, exp) in enumerate(zip(received, expected)):
        assert got == exp, f"byte[{i}]: got 0x{got:02X}, expected 0x{exp:02X}"


@cocotb.test()
async def test_sell_side_byte(dut):
    """Sell order encodes side byte as 0x53 ('S')."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    order = {"order_id": 1, "price": 500_000, "quantity": 50,
              "symbol_id": 1, "side": ORD_SELL}

    await send_order(dut, order)
    received = await collect_bytes(dut, MSG_LEN, timeout=60)

    assert len(received) == MSG_LEN
    assert received[3] == 0x53, f"sell side byte: got 0x{received[3]:02X}, expected 0x53"


@cocotb.test()
async def test_tlast_on_final_byte(dut):
    """tlast asserted only on the 20th (last) byte."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    order = {"order_id": 42, "price": 100, "quantity": 10,
              "symbol_id": 0, "side": ORD_BUY}
    await send_order(dut, order)

    byte_idx   = 0
    tlast_seen = []
    for _ in range(60):
        await RisingEdge(dut.clk)
        if dut.tx_tvalid.value and dut.tx_tready.value:
            if dut.tx_tlast.value:
                tlast_seen.append(byte_idx)
            byte_idx += 1
            if byte_idx == MSG_LEN:
                break

    assert tlast_seen == [MSG_LEN - 1], \
        f"tlast positions: {tlast_seen} (expected [{MSG_LEN - 1}])"


@cocotb.test()
async def test_backpressure(dut):
    """tready deasserted mid-stream: no bytes lost, total still 20."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    order = {"order_id": 0xABCD, "price": 999_999, "quantity": 77,
              "symbol_id": 2, "side": ORD_BUY}
    await send_order(dut, order)

    received = []
    stall_inserted = False
    for _ in range(80):
        await RisingEdge(dut.clk)

        if dut.tx_tvalid.value and dut.tx_tready.value:
            received.append(int(dut.tx_tdata.value))
            if len(received) == MSG_LEN:
                break

        # Stall after collecting 6 bytes: deassert tready for 3 cycles
        if not stall_inserted and len(received) == 6:
            dut.tx_tready.value = 0
            for _ in range(3):
                await RisingEdge(dut.clk)
            dut.tx_tready.value = 1
            stall_inserted = True

    assert len(received) == MSG_LEN, f"got {len(received)} bytes after backpressure"
    expected = build_expected(order)
    for i, (got, exp) in enumerate(zip(received, expected)):
        assert got == exp, f"byte[{i}] after backpressure: 0x{got:02X} != 0x{exp:02X}"


@cocotb.test()
async def test_consecutive_orders(dut):
    """Two back-to-back orders produce correct independent byte streams."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    orders = [
        {"order_id": 1, "price": 100_000, "quantity": 10, "symbol_id": 0, "side": ORD_BUY},
        {"order_id": 2, "price": 200_000, "quantity": 20, "symbol_id": 1, "side": ORD_SELL},
    ]

    # Send and collect each order sequentially — encoder is busy for 20 cycles
    for order in orders:
        await send_order(dut, order)
        received = await collect_bytes(dut, MSG_LEN, timeout=60)
        assert len(received) == MSG_LEN, f"order {order['order_id']}: only {len(received)} bytes"
        expected = build_expected(order)
        for i, (got, exp) in enumerate(zip(received, expected)):
            assert got == exp, \
                f"order {order['order_id']} byte[{i}]: 0x{got:02X} != 0x{exp:02X}"


@cocotb.test()
async def test_sideband_values(dut):
    """UDP sideband: src_port=PORT_OUCH, dst_port=PORT_OUCH, length=20."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    # Check sideband at rest (before any order)
    await ClockCycles(dut.clk, 2)
    assert int(dut.tx_src_port.value) == PORT_OUCH, \
        f"src_port {int(dut.tx_src_port.value)} != {PORT_OUCH}"
    assert int(dut.tx_dst_port.value) == PORT_OUCH, \
        f"dst_port {int(dut.tx_dst_port.value)} != {PORT_OUCH}"
    assert int(dut.tx_length.value)   == MSG_LEN, \
        f"length {int(dut.tx_length.value)} != {MSG_LEN}"
