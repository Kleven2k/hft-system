"""
test_strategy.py
Verifies strategy.sv: spread trigger, round-robin selection,
order field correctness, cooldown enforcement.
Phase 15: adds OMS tests — ACK handling, partial fills, rejects, timeout.
Phase 16: adds pre-trade risk tests — kill switch, fat-finger, rate limiter.

SPREAD_MAX=20000, COOLDOWN_CYC=10, TIMEOUT_CYC=10000,
REFILL_PERIOD=40, MAX_BURST=5 (set in wrapper).
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

CLK_NS    = 8      # 125 MHz
SPREAD_MAX = 20000
COOLDOWN  = 10
ORD_BUY   = 0
ORD_SELL  = 1
ORD_LIMIT = 1

# ACK status codes (must match hft_pkg.sv)
ACK_FILLED    = 0x00
ACK_PARTIAL   = 0x01
ACK_REJECTED  = 0x02
ACK_CANCELLED = 0x03


REFILL_PERIOD = 200   # must match wrapper parameter
SKEW_SHIFT    = 0     # must match wrapper parameter; inv_skew = position >>> SKEW_SHIFT
ORDER_QTY     = 100

def _set_book(dut, slot, bid_price, ask_price, spread,
              mid_price=None, bid_valid=1, ask_valid=1):
    getattr(dut, f"best_bid_price_{slot}").value = bid_price
    getattr(dut, f"best_ask_price_{slot}").value = ask_price
    getattr(dut, f"spread_{slot}").value         = spread
    getattr(dut, f"bid_valid_{slot}").value      = bid_valid
    getattr(dut, f"ask_valid_{slot}").value      = ask_valid
    if mid_price is None:
        mid_price = (bid_price + ask_price) // 2
    getattr(dut, f"mid_price_{slot}").value = mid_price


def _clear_all_books(dut):
    for i in range(4):
        _set_book(dut, i, 0, 0, 0, mid_price=0, bid_valid=0, ask_valid=0)


def _clear_ack(dut):
    dut.ack_valid.value     = 0
    dut.ack_order_id.value  = 0
    dut.ack_status.value    = 0
    dut.ack_fill_qty.value  = 0


async def send_ack(dut, order_id, status, fill_qty=0):
    """Drive a one-cycle ACK pulse into the DUT."""
    dut.ack_valid.value    = 1
    dut.ack_order_id.value = order_id
    dut.ack_status.value   = status
    dut.ack_fill_qty.value = fill_qty
    await RisingEdge(dut.clk)
    _clear_ack(dut)


async def reset_dut(dut):
    dut.rst.value        = 1
    dut.kill_switch.value = 0
    _clear_all_books(dut)
    _clear_ack(dut)
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


async def wait_for_new_order(dut, timeout=20):
    """Wait for order_valid=1 AND order_cancel=0 (new order only)."""
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        if dut.order_valid.value == 1 and dut.order_cancel.value == 0:
            return True
    return False


async def wait_for_cancel(dut, timeout=20):
    """Wait for order_valid=1 AND order_cancel=1 (cancel only)."""
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        if dut.order_valid.value == 1 and dut.order_cancel.value == 1:
            return True
    return False


# ── Existing tests (unchanged behaviour) ──────────────────────────────

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

    fired = await wait_for_new_order(dut, timeout=20)
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

    fired = await wait_for_new_order(dut, timeout=20)
    assert fired, "expected order_valid for slot 2"
    assert int(dut.order_symbol_id.value) == 2, \
        f"expected symbol_id=2, got {int(dut.order_symbol_id.value)}"
    assert int(dut.order_price.value) == bid


@cocotb.test()
async def test_cooldown_prevents_refire(dut):
    """After firing, no second new order until cooldown expires and pending cleared."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    # Wait for first order; capture ID and ACK immediately to clear pending
    fired = await wait_for_new_order(dut, timeout=20)
    assert fired, "first order should fire"
    captured_id = int(dut.order_id.value)
    await send_ack(dut, captured_id, ACK_FILLED, 100)

    # During cooldown no new order should appear
    fires_during_cooldown = 0
    for _ in range(COOLDOWN - 1):
        await RisingEdge(dut.clk)
        if dut.order_valid.value == 1 and dut.order_cancel.value == 0:
            fires_during_cooldown += 1

    assert fires_during_cooldown == 0, \
        f"should not fire during cooldown, got {fires_during_cooldown} fires"

    # After cooldown, should fire again
    fired_again = await wait_for_new_order(dut, timeout=20)
    assert fired_again, "should fire again after cooldown expires"


@cocotb.test()
async def test_order_id_increments(dut):
    """order_id increments with each successive new order."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    ids = []
    for _ in range(3):
        fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
        assert fired, "expected order"
        ids.append(int(dut.order_id.value))
        # ACK immediately to clear pending so next order can fire after cooldown
        await send_ack(dut, ids[-1], ACK_FILLED, 100)

    assert ids[1] == ids[0] + 1, f"id not incrementing: {ids}"
    assert ids[2] == ids[1] + 1, f"id not incrementing: {ids}"


# ── Phase 15 OMS tests ────────────────────────────────────────────────

@cocotb.test()
async def test_oms_full_fill_clears_pending(dut):
    """ACK_FILLED clears pending so a new order can fire after cooldown."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    fired = await wait_for_new_order(dut, timeout=20)
    assert fired, "first order should fire"
    oid = int(dut.order_id.value)
    await send_ack(dut, oid, ACK_FILLED, 100)

    # Now pending is clear; after cooldown a second order should appear
    fired2 = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert fired2, "second order should fire after ACK_FILLED + cooldown"


@cocotb.test()
async def test_oms_partial_fill_then_full(dut):
    """ACK_PARTIAL keeps pending; second ACK_FILLED clears it and fires next order."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    fired = await wait_for_new_order(dut, timeout=20)
    assert fired, "first order should fire"
    oid = int(dut.order_id.value)

    # Partial fill: 60 of 100 shares
    await send_ack(dut, oid, ACK_PARTIAL, 60)

    # No new order yet — still pending with 40 remaining
    no_fire = True
    for _ in range(COOLDOWN - 1):
        await RisingEdge(dut.clk)
        if dut.order_valid.value == 1 and dut.order_cancel.value == 0:
            no_fire = False
    assert no_fire, "new order should not fire while partially filled order is pending"

    # Full fill for remaining 40 shares
    await send_ack(dut, oid, ACK_FILLED, 40)

    # After cooldown, new order fires
    fired2 = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert fired2, "second order should fire after full fill clears pending"


@cocotb.test()
async def test_oms_reject_clears_pending(dut):
    """ACK_REJECTED clears pending so a new order can fire after cooldown."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    fired = await wait_for_new_order(dut, timeout=20)
    assert fired, "first order should fire"
    oid = int(dut.order_id.value)

    # Reject
    await send_ack(dut, oid, ACK_REJECTED, 0)

    # After cooldown, new order fires (reject did not block re-quoting)
    fired2 = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert fired2, "new order should fire after ACK_REJECTED clears pending"


@cocotb.test()
async def test_oms_no_new_order_while_pending(dut):
    """While an order is pending (no ACK), no second new order is emitted."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    fired = await wait_for_new_order(dut, timeout=20)
    assert fired, "first order should fire"

    # Do NOT send ACK — pending stays set.
    # Wait COOLDOWN cycles and verify no second new order fires.
    new_orders = 0
    for _ in range(COOLDOWN + 5):
        await RisingEdge(dut.clk)
        if dut.order_valid.value == 1 and dut.order_cancel.value == 0:
            new_orders += 1

    assert new_orders == 0, \
        f"no new order expected while pending (got {new_orders})"


# ── Phase 16 Pre-trade risk tests ─────────────────────────────────────

@cocotb.test()
async def test_risk_kill_switch_blocks_orders(dut):
    """kill_switch=1 prevents any new order from firing."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    dut.kill_switch.value = 1
    await ClockCycles(dut.clk, 1)

    fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert not fired, "kill_switch=1 should block all new orders"


@cocotb.test()
async def test_risk_kill_switch_resume(dut):
    """After kill_switch clears, orders resume normally."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    _set_book(dut, 0, bid, ask, spread=5000)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    dut.kill_switch.value = 1
    await ClockCycles(dut.clk, COOLDOWN + 5)
    dut.kill_switch.value = 0

    fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert fired, "orders should resume after kill_switch clears"


@cocotb.test()
async def test_risk_fat_finger_blocks_bad_mid(dut):
    """Fat-finger blocks order when mid_price=0 (empty/corrupt book)."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    bid = 1_000_000
    ask = bid + 5000
    # mid_price=0 simulates a corrupt book; fat-finger should block
    _set_book(dut, 0, bid, ask, spread=5000, mid_price=0)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert not fired, "fat-finger should block orders when mid_price=0"


@cocotb.test()
async def test_risk_fat_finger_passes_normal(dut):
    """Fat-finger passes when prices are within FAT_FINGER_BPS of mid."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    mid = 1_000_000
    half_spread = 10
    bid = mid - half_spread
    ask = mid + half_spread
    _set_book(dut, 0, bid, ask, spread=half_spread * 2, mid_price=mid)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert fired, "fat-finger should pass for normal prices near mid"


@cocotb.test()
async def test_risk_rate_limiter_exhausts_tokens(dut):
    """After MAX_BURST orders, rate limiter blocks until tokens refill."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    MAX_BURST    = 5
    mid = 1_000_000
    bid = mid - 10
    ask = mid + 10
    _set_book(dut, 0, bid, ask, spread=20, mid_price=mid)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    # Fire MAX_BURST orders by ACKing each one immediately
    for n in range(MAX_BURST):
        fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
        assert fired, f"order {n+1} should fire (token bucket not yet empty)"
        oid = int(dut.order_id.value)
        await send_ack(dut, oid, ACK_FILLED, 100)

    # Bucket is now empty; next attempt should be blocked —
    # verify no new order fires within one cooldown window (no refill yet)
    await ClockCycles(dut.clk, COOLDOWN + 5)
    blocked = await wait_for_new_order(dut, timeout=COOLDOWN * 2)
    assert not blocked, \
        f"rate limiter should block new order when token bucket empty"


@cocotb.test()
async def test_risk_rate_limiter_refills(dut):
    """After REFILL_PERIOD cycles, a new token arrives and order fires."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    MAX_BURST = 5
    mid = 1_000_000
    bid = mid - 10
    ask = mid + 10
    _set_book(dut, 0, bid, ask, spread=20, mid_price=mid)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    # Drain all tokens
    for _ in range(MAX_BURST):
        fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
        assert fired, "expected order while tokens available"
        oid = int(dut.order_id.value)
        await send_ack(dut, oid, ACK_FILLED, 100)

    # Token bucket empty — wait up to one full refill period for the next order
    fired = await wait_for_new_order(dut, timeout=REFILL_PERIOD + COOLDOWN + 10)
    assert fired, "order should fire after token refill"


# ── Phase 17 Inventory skew tests ─────────────────────────────────────

@cocotb.test()
async def test_inv_skew_long_skews_ask_down(dut):
    """After a BUY fill (long inventory), SELL ask is skewed down by inv_skew."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    mid = 1_000_000
    bid = mid - 50
    ask = mid + 50
    _set_book(dut, 0, bid, ask, spread=100, mid_price=mid)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    # First order: BUY at bid (position=0, skew=0)
    fired = await wait_for_new_order(dut, timeout=20)
    assert fired and int(dut.order_side.value) == ORD_BUY
    assert int(dut.order_price.value) == bid, "first BUY should be at unskewed bid"
    oid = int(dut.order_id.value)
    await send_ack(dut, oid, ACK_FILLED, ORDER_QTY)  # position = +100

    # Second order: SELL — skew = 100*100/100 = 100 → ask - 100
    fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert fired and int(dut.order_side.value) == ORD_SELL
    expected = ask - (ORDER_QTY >> SKEW_SHIFT)  # = ask - 100 with SKEW_SHIFT=0
    assert int(dut.order_price.value) == expected, \
        f"long skew: expected ask-100={expected}, got {int(dut.order_price.value)}"


@cocotb.test()
async def test_inv_skew_short_skews_bid_up(dut):
    """After a SELL fill (short inventory), BUY bid is skewed up by |inv_skew|."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    mid = 1_000_000
    bid = mid - 50
    ask = mid + 50
    _set_book(dut, 0, bid, ask, spread=100, mid_price=mid)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, bid_valid=0, ask_valid=0)

    # Step 1: BUY fires → ACK_REJECTED so position stays 0, sell_turn → 1
    fired = await wait_for_new_order(dut, timeout=20)
    assert fired and int(dut.order_side.value) == ORD_BUY
    oid = int(dut.order_id.value)
    await send_ack(dut, oid, ACK_REJECTED)  # position unchanged = 0

    # Step 2: SELL fires at unskewed ask (position=0) → ACK_FILLED → position = -100
    fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert fired and int(dut.order_side.value) == ORD_SELL
    assert int(dut.order_price.value) == ask, "SELL before any fill should be at unskewed ask"
    oid = int(dut.order_id.value)
    await send_ack(dut, oid, ACK_FILLED, ORDER_QTY)  # position = -100

    # Step 3: BUY fires — skew = -100*100/100 = -100 → bid - (-100) = bid + 100
    fired = await wait_for_new_order(dut, timeout=COOLDOWN + 10)
    assert fired and int(dut.order_side.value) == ORD_BUY
    expected = bid + (ORDER_QTY >> SKEW_SHIFT)  # = bid + 100 with SKEW_SHIFT=0
    assert int(dut.order_price.value) == expected, \
        f"short skew: expected bid+100={expected}, got {int(dut.order_price.value)}"


# ── Phase 22 Dynamic spread filter tests ──────────────────────────────────────

@cocotb.test()
async def test_dynamic_spread_filter_blocks_sudden_wide(dut):
    """
    Phase 22: EMA filter blocks quoting when spread suddenly widens above 1.5×EMA.

    SPREAD_EMA_SHIFT=4 (alpha=1/16), SPREAD_MAX=20000, COOLDOWN=10.

    Phase A — EMA convergence under kill_switch=1:
      No orders fire → tokens=5 intact, cooldown stays 0.
      After 150 cycles of spread=100: EMA ≈ 100, threshold ≈ 150.

    Phase B — wide spread=5000 (< SPREAD_MAX=20000 but >> threshold≈150):
      kill_switch lowered after 1-cycle spread_r propagation delay.
      EMA blocking window ≈17 cycles >> COOLDOWN+4=14 → no order fires.

    Phase C — re-narrow spread=100:
      EMA ≈3040 (rose during wide phase), threshold ≈4560 >> 100.
      cooldown=0, pending=0, tokens=5 → order fires within COOLDOWN+5.
    """
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    mid = 1_000_000
    bid = mid - 50
    ask = mid + 50
    normal_spread = 100

    _set_book(dut, 0, bid, ask, spread=normal_spread, mid_price=mid)
    for i in range(1, 4):
        _set_book(dut, i, 0, 0, 0, mid_price=0, bid_valid=0, ask_valid=0)

    # ---- Phase A: EMA convergence under kill_switch ----
    # kill_switch=1 prevents all order sends → tokens stay at MAX_BURST=5,
    # cooldown stays 0. After 150 cycles of spread=100: EMA ≈ 100, threshold ≈ 150.
    dut.kill_switch.value = 1
    await ClockCycles(dut.clk, 150)

    # ---- Phase B: Sudden wide spread — EMA filter must block ----
    # Set wide spread while kill_switch is still 1, then wait 1 cycle for
    # spread_r to register, then lower kill_switch.  This closes the
    # 1-cycle window where spread_r could still be 100 on the first eval.
    wide_spread = 5000
    _set_book(dut, 0, bid - wide_spread // 2, ask + wide_spread // 2,
              spread=wide_spread, mid_price=mid)
    await ClockCycles(dut.clk, 1)   # spread_r propagates; EMA advances once → ≈406
    dut.kill_switch.value = 0       # enable orders; threshold ≈609 << 5000

    # EMA blocking window ≈17 cycles; we check only COOLDOWN+4=14.
    fired = await wait_for_new_order(dut, timeout=COOLDOWN + 4)
    assert not fired, (
        f"EMA filter should block spread={wide_spread} (>> 1.5×EMA≈150), "
        f"but order fired"
    )

    # ---- Phase C: Re-narrow — quoting must resume ----
    # After 14 wide cycles: EMA ≈3040, threshold ≈4560 >> 100.
    # cooldown=0 (nothing ever fired), pending=0, tokens=5.
    _set_book(dut, 0, bid, ask, spread=normal_spread, mid_price=mid)
    fired = await wait_for_new_order(dut, timeout=COOLDOWN + 5)
    assert fired, "Expected quoting to resume after spread normalises"
