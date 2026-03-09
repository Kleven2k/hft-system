"""
test_order_book.py
Verifies order_book.sv: price level tracking, best bid/ask,
spread/mid computation, cancel/execute drain, and scan recovery.

Price model:
  price_base=100, tick=1. Prices are small integers 100..355,
  fitting naturally in the 256-level window.
  price_idx = quote_price - price_base  (0..255)
  best_bid_price output = price_base + best_bid_idx
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

CLK_NS = 8    # 125 MHz
BASE   = 100  # price_base: index 0 maps to absolute price 100
TICK   = 1    # one tick = one price unit

OP_ADD     = 0
OP_CANCEL  = 1
OP_EXECUTE = 2


async def reset_dut(dut):
    dut.rst.value           = 1
    dut.quote_valid.value   = 0
    dut.quote_is_bid.value  = 0
    dut.quote_price.value   = 0
    dut.quote_shares.value  = 0
    dut.quote_symbol_id.value = 0
    dut.quote_op.value      = 0
    dut.quote_timestamp.value = 0
    dut.price_base.value    = BASE
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 260)  # wait for BRAM clear (MAX_LEVELS=256 + margin)


async def send_quote(dut, price, shares, is_bid, op=OP_ADD, symbol_id=0):
    await RisingEdge(dut.clk)
    dut.quote_valid.value     = 1
    dut.quote_price.value     = price
    dut.quote_shares.value    = shares
    dut.quote_is_bid.value    = is_bid
    dut.quote_op.value        = op
    dut.quote_symbol_id.value = symbol_id
    dut.quote_timestamp.value = 0
    await RisingEdge(dut.clk)
    dut.quote_valid.value = 0


async def settle(dut, cycles=3):
    """Wait for registered outputs to propagate."""
    await ClockCycles(dut.clk, cycles)


# ── Tests ────────────────────────────────────────────────────

@cocotb.test()
async def test_single_bid_add(dut):
    """Single bid ADD sets best_bid_price and qty correctly."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    price = BASE + 50 * TICK  # $90.50
    await send_quote(dut, price, 100, is_bid=1, op=OP_ADD)
    await settle(dut)

    assert dut.bid_valid.value  == 1,     "bid_valid not set"
    assert int(dut.best_bid_price.value) == price, \
        f"best_bid_price {int(dut.best_bid_price.value)} != {price}"
    assert int(dut.best_bid_qty.value)   == 100, \
        f"best_bid_qty {int(dut.best_bid_qty.value)} != 100"
    assert dut.ask_valid.value  == 0,     "ask_valid should be 0"


@cocotb.test()
async def test_single_ask_add(dut):
    """Single ask ADD sets best_ask_price and qty correctly."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    price = BASE + 51 * TICK  # $90.51
    await send_quote(dut, price, 200, is_bid=0, op=OP_ADD)
    await settle(dut)

    assert dut.ask_valid.value  == 1
    assert int(dut.best_ask_price.value) == price
    assert int(dut.best_ask_qty.value)   == 200
    assert dut.bid_valid.value  == 0


@cocotb.test()
async def test_spread_and_mid(dut):
    """Spread and mid-price computed correctly from bid+ask."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid_price = BASE + 50 * TICK   # $90.50
    ask_price = BASE + 52 * TICK   # $90.52

    await send_quote(dut, bid_price, 100, is_bid=1)
    await send_quote(dut, ask_price, 100, is_bid=0)
    await settle(dut)

    expected_spread = ask_price - bid_price          # 2 ticks = 20000
    expected_mid    = (bid_price + ask_price) // 2   # $90.51

    assert int(dut.spread.value)    == expected_spread, \
        f"spread {int(dut.spread.value)} != {expected_spread}"
    assert int(dut.mid_price.value) == expected_mid, \
        f"mid {int(dut.mid_price.value)} != {expected_mid}"


@cocotb.test()
async def test_best_bid_improves(dut):
    """Higher bid price replaces best_bid."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    await send_quote(dut, BASE + 40 * TICK, 100, is_bid=1)
    await send_quote(dut, BASE + 50 * TICK, 200, is_bid=1)  # better bid
    await settle(dut)

    assert int(dut.best_bid_price.value) == BASE + 50 * TICK, \
        "best_bid should have moved to higher price"
    assert int(dut.best_bid_qty.value)   == 200


@cocotb.test()
async def test_best_ask_improves(dut):
    """Lower ask price replaces best_ask."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    await send_quote(dut, BASE + 60 * TICK, 100, is_bid=0)
    await send_quote(dut, BASE + 52 * TICK, 300, is_bid=0)  # better ask
    await settle(dut)

    assert int(dut.best_ask_price.value) == BASE + 52 * TICK
    assert int(dut.best_ask_qty.value)   == 300


@cocotb.test()
async def test_qty_accumulates(dut):
    """Multiple ADDs at same price level accumulate quantity."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    price = BASE + 50 * TICK
    await send_quote(dut, price, 100, is_bid=1)
    await send_quote(dut, price, 200, is_bid=1)
    await send_quote(dut, price, 150, is_bid=1)
    await settle(dut)

    assert int(dut.best_bid_qty.value) == 450, \
        f"Expected 450 shares, got {int(dut.best_bid_qty.value)}"


@cocotb.test()
async def test_cancel_partial(dut):
    """CANCEL reduces qty at level, best_bid unchanged if level not drained."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    price = BASE + 50 * TICK
    await send_quote(dut, price, 500, is_bid=1, op=OP_ADD)
    await send_quote(dut, price, 200, is_bid=1, op=OP_CANCEL)
    await settle(dut)

    assert dut.bid_valid.value == 1
    assert int(dut.best_bid_price.value) == price
    assert int(dut.best_bid_qty.value)   == 300


@cocotb.test()
async def test_cancel_drains_and_scan_recovers(dut):
    """Cancelling best bid triggers scan; next best bid found correctly."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    price_lo = BASE + 40 * TICK  # $90.40 — second best
    price_hi = BASE + 50 * TICK  # $90.50 — best bid

    await send_quote(dut, price_lo, 300, is_bid=1, op=OP_ADD)
    await send_quote(dut, price_hi, 100, is_bid=1, op=OP_ADD)
    await settle(dut)

    assert int(dut.best_bid_price.value) == price_hi

    # Cancel entire best bid level
    await send_quote(dut, price_hi, 100, is_bid=1, op=OP_CANCEL)

    # Scan takes up to MAX_LEVELS cycles — wait generously
    await ClockCycles(dut.clk, 30)

    assert dut.bid_valid.value == 1, "bid_valid should recover after scan"
    assert int(dut.best_bid_price.value) == price_lo, \
        f"best_bid should fall back to {price_lo}, got {int(dut.best_bid_price.value)}"
    assert int(dut.best_bid_qty.value) == 300


@cocotb.test()
async def test_execute_drains_ask(dut):
    """EXECUTE reduces ask qty; full drain triggers scan to next ask level."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    price_lo = BASE + 51 * TICK  # best ask
    price_hi = BASE + 55 * TICK  # second best ask

    await send_quote(dut, price_lo, 100, is_bid=0, op=OP_ADD)
    await send_quote(dut, price_hi, 400, is_bid=0, op=OP_ADD)
    await settle(dut)

    assert int(dut.best_ask_price.value) == price_lo

    # Execute the full best ask
    await send_quote(dut, price_lo, 100, is_bid=0, op=OP_EXECUTE)
    await ClockCycles(dut.clk, 30)

    assert dut.ask_valid.value == 1
    assert int(dut.best_ask_price.value) == price_hi
    assert int(dut.best_ask_qty.value)   == 400


@cocotb.test()
async def test_both_sides_drained(dut):
    """After all bids and asks cancelled, both valid flags go low."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    price_b = BASE + 50 * TICK
    price_a = BASE + 51 * TICK

    await send_quote(dut, price_b, 100, is_bid=1, op=OP_ADD)
    await send_quote(dut, price_a, 100, is_bid=0, op=OP_ADD)
    await settle(dut)

    await send_quote(dut, price_b, 100, is_bid=1, op=OP_CANCEL)
    await send_quote(dut, price_a, 100, is_bid=0, op=OP_CANCEL)
    await ClockCycles(dut.clk, 30)

    assert dut.bid_valid.value == 0, "bid_valid should be 0 after full cancel"
    assert dut.ask_valid.value == 0, "ask_valid should be 0 after full cancel"
    assert int(dut.spread.value)    == 0
    assert int(dut.mid_price.value) == 0