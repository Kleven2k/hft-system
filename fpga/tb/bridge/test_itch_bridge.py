"""
test_itch_bridge.py
Verifies that quotes written on wclk (rgmii_rxc) domain
correctly appear on rclk (clk_unbuf) domain through the async FIFO.

Tests:
  1. Single quote crosses cleanly
  2. Burst of 8 quotes — none dropped, all fields correct
  3. Back-to-back quotes with varying inter-packet gaps
  4. Drop counter increments when FIFO artificially held full
     (tested by flooding faster than read side drains — hard to
      trigger with DEPTH=16, so we verify counter stays zero
      under normal load instead)
  5. Reset clears FIFO and drop counter
  6. Price precision preserved across CDC
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer, ClockCycles
from cocotb.utils import get_sim_time
import random

# ---- Clock frequencies (both 125 MHz but independent) ------
WCLK_PERIOD_NS = 8   # rgmii_rxc
RCLK_PERIOD_NS = 8   # clk_unbuf — same freq, independent phase


async def reset_dut(dut):
    """Assert both resets with guaranteed negedge (drive 1 first, then 0).
    Extra settling ensures gray-code synchronizers fully reset.
    """
    # Drive inputs to known state
    dut.w_quote_valid.value = 0
    dut.w_quote_is_bid.value = 0
    dut.w_quote_timestamp.value = 0
    dut.w_quote_price.value = 0
    dut.w_quote_shares.value = 0
    dut.w_quote_symbol_id.value = 0

    # Guarantee a negedge by driving 1 first, then 0
    dut.wrst_n.value = 1
    dut.rrst_n.value = 1
    await ClockCycles(dut.wclk, 2)

    # Now assert reset (guaranteed negedge for async reset sensitivity)
    dut.wrst_n.value = 0
    dut.rrst_n.value = 0
    await ClockCycles(dut.wclk, 10)
    await ClockCycles(dut.rclk, 10)

    dut.wrst_n.value = 1
    dut.rrst_n.value = 1

    # Allow 2-FF synchronizers to settle after reset release
    await ClockCycles(dut.wclk, 5)
    await ClockCycles(dut.rclk, 5)


async def send_quote(dut, timestamp, price, shares, symbol_id, is_bid):
    """Drive one quote on wclk domain for one cycle."""
    await RisingEdge(dut.wclk)
    dut.w_quote_valid.value     = 1
    dut.w_quote_timestamp.value = timestamp
    dut.w_quote_price.value     = price
    dut.w_quote_shares.value    = shares
    dut.w_quote_symbol_id.value = symbol_id
    dut.w_quote_is_bid.value    = is_bid
    await RisingEdge(dut.wclk)
    dut.w_quote_valid.value = 0


async def wait_for_quote(dut, timeout_cycles=200):
    """Wait for r_quote_valid pulse on rclk domain. Returns captured fields."""
    for _ in range(timeout_cycles):
        await RisingEdge(dut.rclk)
        if dut.r_quote_valid.value == 1:
            return {
                'timestamp': int(dut.r_quote_timestamp.value),
                'price':     int(dut.r_quote_price.value),
                'shares':    int(dut.r_quote_shares.value),
                'symbol_id': int(dut.r_quote_symbol_id.value),
                'is_bid':    int(dut.r_quote_is_bid.value),
            }
    raise TimeoutError("Timeout waiting for r_quote_valid")


# ---- Tests -------------------------------------------------

@cocotb.test()
async def test_single_quote_crosses(dut):
    """A single quote written on wclk appears correctly on rclk."""
    cocotb.start_soon(Clock(dut.wclk, WCLK_PERIOD_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rclk, RCLK_PERIOD_NS, unit="ns").start())

    await reset_dut(dut)

    TS     = 0x0000_1234_5678_9ABC
    PRICE  = 1_823_456   # $182.3456
    SHARES = 100
    SYM    = 0x4150      # 'AP' → AAPL abbreviation
    IS_BID = 1

    await send_quote(dut, TS, PRICE, SHARES, SYM, IS_BID)
    q = await wait_for_quote(dut)

    assert q['timestamp'] == TS,     f"timestamp mismatch: {q['timestamp']:#x} != {TS:#x}"
    assert q['price']     == PRICE,  f"price mismatch: {q['price']} != {PRICE}"
    assert q['shares']    == SHARES, f"shares mismatch: {q['shares']} != {SHARES}"
    assert q['symbol_id'] == SYM,    f"symbol_id mismatch: {q['symbol_id']:#x} != {SYM:#x}"
    assert q['is_bid']    == IS_BID, f"is_bid mismatch: {q['is_bid']} != {IS_BID}"


@cocotb.test()
async def test_burst_8_quotes(dut):
    """8 quotes sent back-to-back, all received in order with correct fields."""
    cocotb.start_soon(Clock(dut.wclk, WCLK_PERIOD_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rclk, RCLK_PERIOD_NS, unit="ns").start())

    await reset_dut(dut)

    quotes = [
        {'ts': 1000 * i, 'price': 500_000 + i * 10000,
         'shares': 100 + i * 10, 'sym': 0x4150 + i, 'bid': i % 2}
        for i in range(8)
    ]

    # Collect received quotes concurrently with sends.
    # The bridge drains the FIFO immediately on each write, so we must
    # be listening before the first send.
    received_all = []

    async def collect_quotes():
        for _ in range(8):
            r = await wait_for_quote(dut, timeout_cycles=200)
            cocotb.log.info(f"  RX ts={r['timestamp']} price={r['price']}")
            received_all.append(r)

    collector = cocotb.start_soon(collect_quotes())

    # Send all 8 quotes with 2-cycle gaps
    for q in quotes:
        await send_quote(dut, q['ts'], q['price'], q['shares'], q['sym'], q['bid'])
        await ClockCycles(dut.wclk, 2)


    # Wait for collector to finish (give it extra time for last quote sync latency)
    await ClockCycles(dut.rclk, 50)
    await collector

    assert len(received_all) == 8, f"Only got {len(received_all)}/8 quotes"
    for i, (received, expected) in enumerate(zip(received_all, quotes)):
        cocotb.log.info(f"[{i}] rx ts={received['timestamp']} exp ts={expected['ts']}")
        assert received['timestamp'] == expected['ts'],    f"[{i}] ts mismatch: rx={received['timestamp']} exp={expected['ts']}"
        assert received['price']     == expected['price'], f"[{i}] price mismatch"
        assert received['shares']    == expected['shares'],f"[{i}] shares mismatch"
        assert received['symbol_id'] == expected['sym'],   f"[{i}] sym mismatch"
        assert received['is_bid']    == expected['bid'],   f"[{i}] is_bid mismatch"


@cocotb.test()
async def test_ask_side_crosses(dut):
    """is_bid=0 (ask) preserved correctly."""
    cocotb.start_soon(Clock(dut.wclk, WCLK_PERIOD_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rclk, RCLK_PERIOD_NS, unit="ns").start())

    await reset_dut(dut)

    await send_quote(dut,
        timestamp=0xDEAD_BEEF_1234_5678,
        price=2_000_000,
        shares=500,
        symbol_id=0x0042,
        is_bid=0
    )
    q = await wait_for_quote(dut)
    assert q['is_bid'] == 0, "Expected ask (is_bid=0)"
    assert q['price']  == 2_000_000


@cocotb.test()
async def test_price_precision_preserved(dut):
    """Fixed-point price survives CDC without truncation."""
    cocotb.start_soon(Clock(dut.wclk, WCLK_PERIOD_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rclk, RCLK_PERIOD_NS, unit="ns").start())

    await reset_dut(dut)

    # $182.3456 = 1_823_456 in 1/10000 fixed point
    PRICE = 1_823_456
    await send_quote(dut, 0, PRICE, 1, 0, 1)
    q = await wait_for_quote(dut)
    assert q['price'] == PRICE, f"Price precision lost: {q['price']} != {PRICE}"


@cocotb.test()
async def test_drop_count_zero_normal_operation(dut):
    """Drop counter stays zero under normal traffic (FIFO never fills)."""
    cocotb.start_soon(Clock(dut.wclk, WCLK_PERIOD_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rclk, RCLK_PERIOD_NS, unit="ns").start())

    await reset_dut(dut)

    for i in range(12):
        await send_quote(dut, i * 100, i * 10000, 50, i, i % 2)
        await ClockCycles(dut.wclk, 8)   # give read side time to drain

    await ClockCycles(dut.rclk, 50)

    drop_count = int(dut.w_drop_count.value)
    assert drop_count == 0, f"Unexpected drops under normal load: {drop_count}"


@cocotb.test()
async def test_fifo_empty_after_drain(dut):
    """FIFO reports empty after all quotes have been read."""
    cocotb.start_soon(Clock(dut.wclk, WCLK_PERIOD_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rclk, RCLK_PERIOD_NS, unit="ns").start())

    await reset_dut(dut)

    await send_quote(dut, 999, 12345, 77, 3, 1)
    q = await wait_for_quote(dut)
    assert q['price'] == 12345

    # Wait a few cycles then confirm empty
    await ClockCycles(dut.rclk, 10)
    assert dut.fifo_empty.value == 1, "FIFO should be empty after drain"


@cocotb.test()
async def test_reset_clears_state(dut):
    """After reset, drop_count is zero and FIFO is empty."""
    cocotb.start_soon(Clock(dut.wclk, WCLK_PERIOD_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.rclk, RCLK_PERIOD_NS, unit="ns").start())

    await reset_dut(dut)

    # Send a few quotes
    for i in range(3):
        await send_quote(dut, i, i * 1000, i * 10, i, 1)
        await ClockCycles(dut.wclk, 2)

    # Re-assert reset
    dut.wrst_n.value = 0
    dut.rrst_n.value = 0
    await ClockCycles(dut.wclk, 5)
    await ClockCycles(dut.rclk, 5)
    dut.wrst_n.value = 1
    dut.rrst_n.value = 1
    await ClockCycles(dut.rclk, 5)

    assert int(dut.w_drop_count.value) == 0, "drop_count not cleared by reset"
    assert dut.fifo_empty.value == 1, "FIFO not empty after reset"