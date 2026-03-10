"""
test_strategy.py
Verifies strategy.sv: spread trigger, round-robin selection,
order field correctness, cooldown enforcement.

SPREAD_MAX=20000, COOLDOWN_CYC=10 (set in wrapper).
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

CLK_NS    = 8      # 125 MHz
SPREAD_MAX = 20000
COOLDOWN  = 10
ORD_BUY   = 0
ORD_LIMIT = 1


def _set_book(dut, slot, bid_price, ask_price, spread, bid_valid=1, ask_valid=1):
    getattr(dut, f"best_bid_price_{slot}").value = bid_price
    getattr(dut, f"best_ask_price_{slot}").value = ask_price
    getattr(dut, f"spread_{slot}").value         = spread
    getattr(dut, f"bid_valid_{slot}").value      = bid_valid
    getattr(dut, f"ask_valid_{slot}").value      = ask_valid


def _clear_all_books(dut):
    for i in range(4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)


async def reset_dut(dut):
    dut.rst.value = 1
    _clear_all_books(dut)
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)


async def wait_for_order(dut, timeout=20):
    """Wait up to `timeout` cycles for order_valid; return True if seen."""
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        if dut.order_valid.value == 1:
            return True
    return False


# ── Tests ──────────────────────────────────────────────────────

@cocotb.test()
async def test_no_fire_when_books_empty(dut):
    """No order emitted when all books have no valid bid/ask."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)
    _clear_all_books(dut)

    fired = await wait_for_order(dut, timeout=20)
    assert not fired, "order_valid should not fire with empty books"


@cocotb.test()
async def test_no_fire_wide_spread(dut):
    """No order when spread > SPREAD_MAX even with valid bid/ask."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1000
    ask = bid + SPREAD_MAX + 1   # one tick too wide
    _set_book(dut, 0, bid, ask, spread=SPREAD_MAX + 1)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    fired = await wait_for_order(dut, timeout=20)
    assert not fired, "order_valid should not fire on wide spread"


@cocotb.test()
async def test_fires_on_tight_spread(dut):
    """Order fires when spread == SPREAD_MAX (boundary)."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + SPREAD_MAX
    _set_book(dut, 0, bid, ask, spread=SPREAD_MAX)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    fired = await wait_for_order(dut, timeout=20)
    assert fired, "order_valid should fire at spread == SPREAD_MAX"


@cocotb.test()
async def test_order_fields(dut):
    """Order carries correct price (best_bid), qty=100, side=BUY, type=LIMIT."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 500_000
    ask = bid + 10000  # tight spread
    _set_book(dut, 0, bid, ask, spread=10000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    fired = await wait_for_order(dut, timeout=20)
    assert fired, "expected order_valid"

    assert int(dut.order_price.value)     == bid,     f"price {int(dut.order_price.value)} != {bid}"
    assert int(dut.order_qty.value)       == 100,     f"qty   {int(dut.order_qty.value)} != 100"
    assert int(dut.order_side.value)      == ORD_BUY, f"side  {int(dut.order_side.value)} != BUY"
    assert int(dut.order_type.value)      == ORD_LIMIT
    assert int(dut.order_symbol_id.value) == 0,       "symbol_id should be slot 0"


@cocotb.test()
async def test_round_robin_slot_selection(dut):
    """Order generated for slot 2 (only slot 2 has tight spread)."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    for i in range(4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    bid = 800_000
    ask = bid + 5000
    _set_book(dut, 2, bid, ask, spread=5000)

    fired = await wait_for_order(dut, timeout=20)
    assert fired, "expected order_valid for slot 2"
    assert int(dut.order_symbol_id.value) == 2, \
        f"expected symbol_id=2, got {int(dut.order_symbol_id.value)}"
    assert int(dut.order_price.value) == bid


@cocotb.test()
async def test_cooldown_prevents_refire(dut):
    """After firing, no second order until cooldown expires."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    # Wait for first order
    fired = await wait_for_order(dut, timeout=20)
    assert fired, "first order should fire"

    # During cooldown (COOLDOWN_CYC=10), the round-robin hits slot 0 again
    # but should not fire. Check for exactly 0 fires in the next COOLDOWN cycles.
    fires_during_cooldown = 0
    for _ in range(COOLDOWN - 1):
        await RisingEdge(dut.clk)
        if dut.order_valid.value == 1:
            fires_during_cooldown += 1

    assert fires_during_cooldown == 0, \
        f"should not fire during cooldown, got {fires_during_cooldown} fires"

    # After cooldown, should fire again
    fired_again = await wait_for_order(dut, timeout=20)
    assert fired_again, "should fire again after cooldown expires"


@cocotb.test()
async def test_order_id_increments(dut):
    """order_id increments with each successive order."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    ids = []
    for _ in range(3):
        fired = await wait_for_order(dut, timeout=COOLDOWN + 10)
        assert fired, "expected order"
        ids.append(int(dut.order_id.value))
        await ClockCycles(dut.clk, 1)  # consume this pulse

    assert ids[1] == ids[0] + 1, f"id not incrementing: {ids}"
    assert ids[2] == ids[1] + 1, f"id not incrementing: {ids}"
