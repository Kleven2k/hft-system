"""
test_nasdaq.py — Replay real NASDAQ ITCH tick data into strategy.sv (Phase 31)

Feeds real AAPL/MSFT/AMD BookTick CSV rows (from research/data/nasdaq/) directly
into strategy.sv's book inputs via strategy_tb_wrapper.sv (slot 0 only), and
acks fills using the same FIFO queue-position model validated in
research/backtest/nasdaq_mm_backtest.py — i.e. a resting order joins the BACK
of the queue at its price and only fills once the size ahead of it (by
executions or cancels) has left AND a further execution trades through.

This is NOT a replay through the real ITCH wire format / market_data_parser —
it drives strategy.sv's already-parsed book inputs directly, the same way
test_strategy.py does. The wire-format path is exercised separately by
software/itch_replay.py against real hardware.

Prices are converted to integer ticks: price_int = round(usd / TICK_SIZE_USD).

KNOWN DISCREPANCY vs nasdaq_mm_backtest.py (documented, not a bug to fix here):
  strategy.sv's stale check compares bid-price drift (for a BUY; ask for a
  SELL) against the quoted price. nasdaq_mm_backtest.py compares MID-price
  drift against the mid at quote time. These are different quantities that
  diverge whenever the spread changes shape, not just level -- so order
  counts and exact fill timing won't match 1:1 (observed ~8% fewer orders
  in RTL on a 100K-row AAPL slice: 3,507 vs 3,811), even though P&L, fill
  rate, and ending position all land in the same range. Reconciling them
  exactly would mean either re-deriving the Python sweep with a bid/ask-
  based stale check, or changing strategy.sv's timing-critical stale
  pipeline (which crypto also depends on) -- both bigger jobs than this
  testbench's purpose (proving the RTL mechanism works on real NASDAQ data
  and is profitable), so left as a known gap.

Usage:
    python runner_nasdaq.py                     # AAPL, offset=0, stale=2
    python runner_nasdaq.py -- --symbol MSFT
"""

import csv
import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles

CLK_NS        = 8        # 125 MHz (one cycle = 8 ns)
TICK_SIZE_USD = 0.01      # NASDAQ tick size for these symbols
ORDER_QTY     = 100       # must match nasdaq_tb_wrapper.sv's strategy ORDER_QTY
MAKER_REBATE_PER_SHARE = 0.002   # $/share, matches nasdaq_mm_backtest.py

# strategy.sv's stale-detection pipeline is ~3 cycles deep (best_bid_price ->
# bid_p_r -> bid_abs_diff_r -> bid_stale_r). In real hardware, market ticks
# arrive microseconds+ apart (thousands of cycles), so that latency is
# negligible. Holding each row for only 1 cycle (as a naive "one row per
# clock" replay would) makes the pipeline lag distort results. Convert each
# row's real inter-tick gap into cycles instead, capped so a quiet period
# doesn't blow up simulation time — the cap only needs to stay well above
# the ~3-cycle pipeline depth to make it negligible, not match real gaps.
MIN_HOLD_CYCLES = 4
MAX_HOLD_CYCLES = 16   # ~5x pipeline depth is enough to make the lag negligible;
                       # cocotb/Icarus per-cycle overhead makes 200 impractically slow

ACK_FILLED    = 0x00
ACK_CANCELLED = 0x03

DEFAULT_CSV = (Path(__file__).resolve().parents[3] / "research" / "data" / "nasdaq"
               / "aapl_20200130_ticks.csv")
MAX_ROWS = int(os.environ.get("NASDAQ_TB_MAX_ROWS", "200000"))  # cap sim time


def usd_to_ticks(usd: float) -> int:
    return round(usd / TICK_SIZE_USD)


def hold_cycles_for_gap(gap_ns: int) -> int:
    cycles = gap_ns // CLK_NS
    return max(MIN_HOLD_CYCLES, min(MAX_HOLD_CYCLES, cycles))


def load_rows(path: Path, max_rows: int):
    """Load BookTick rows: (timestamp_ns, event, best_bid_ticks, best_ask_ticks, bid_size, ask_size, last_trade_ticks)."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            bid = float(row["best_bid"]) if row["best_bid"] else None
            ask = float(row["best_ask"]) if row["best_ask"] else None
            if bid is None or ask is None:
                continue
            lt = float(row["last_trade"]) if row["last_trade"] else None
            rows.append((
                int(row["ts_ns"]),
                row["event"],
                usd_to_ticks(bid),
                usd_to_ticks(ask),
                int(row["bid_size"]) if row["bid_size"] else 0,
                int(row["ask_size"]) if row["ask_size"] else 0,
                usd_to_ticks(lt) if lt is not None else None,
            ))
    return rows


def _set_book(dut, bid, ask, bid_valid=1, ask_valid=1):
    dut.best_bid_price_0.value = bid
    dut.best_ask_price_0.value = ask
    dut.mid_price_0.value      = (bid + ask) // 2
    dut.spread_0.value         = max(0, ask - bid)
    dut.bid_valid_0.value      = bid_valid
    dut.ask_valid_0.value      = ask_valid


async def send_ack(dut, order_id, status, fill_qty=0):
    dut.ack_valid.value    = 1
    dut.ack_order_id.value = order_id
    dut.ack_status.value   = status
    dut.ack_fill_qty.value = fill_qty
    await RisingEdge(dut.clk)
    dut.ack_valid.value = 0


async def reset_dut(dut):
    dut.rst.value         = 1
    dut.kill_switch.value = 0
    _set_book(dut, 0, 0, bid_valid=0, ask_valid=0)
    dut.ack_valid.value    = 0
    dut.ack_order_id.value = 0
    dut.ack_status.value   = 0
    dut.ack_fill_qty.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 1)


@cocotb.test()
async def test_nasdaq_replay(dut):
    """Replay real tick data, ack via FIFO queue-position model, report P&L."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)

    csv_path = Path(os.environ.get("NASDAQ_TB_CSV", str(DEFAULT_CSV)))
    rows = load_rows(csv_path, MAX_ROWS)
    assert rows, f"No rows loaded from {csv_path}"
    print(f"Loaded {len(rows)} rows from {csv_path.name}")

    # ---- Queue-position / fill state (mirrors nasdaq_mm_backtest.py) -----
    pending          = False
    pending_id       = 0
    pending_side     = None     # "BUY" / "SELL"
    pending_price    = 0
    pending_mid      = 0
    queue_ahead      = 0
    last_size_at_price = 0

    n_orders = 0
    n_fills  = 0
    n_cancels = 0
    n_queue_misses = 0
    realized_pnl_usd = 0.0
    position   = 0
    cost_basis = 0.0
    last_mid   = 0.0

    prev_ts = None
    for row_idx, (ts_ns, event, bid, ask, bid_size, ask_size, last_trade) in enumerate(rows):
        mid = (bid + ask) / 2.0
        last_mid = mid
        _set_book(dut, bid, ask)

        hold = hold_cycles_for_gap(ts_ns - prev_ts) if prev_ts is not None else MIN_HOLD_CYCLES
        prev_ts = ts_ns

        # Hold this row's book state for `hold` cycles (approximating the real
        # inter-tick gap so the ~3-cycle stale-detection pipeline is
        # negligible, as it would be in real hardware). Watch every cycle for
        # the DUT placing or cancelling its own order. Multiple lifecycle
        # events (e.g. a stale-cancel immediately followed by a requote) can
        # legitimately happen within one row's multi-cycle hold window, so
        # this reacts to every cycle rather than capping at one event/row.
        for _ in range(hold):
            await RisingEdge(dut.clk)
            if dut.order_valid.value != 1:
                continue
            if dut.order_cancel.value == 0 and not pending:
                pending       = True
                pending_id    = int(dut.order_id.value)
                pending_side  = "BUY" if int(dut.order_side.value) == 0 else "SELL"
                pending_price = int(dut.order_price.value)
                pending_mid   = mid
                queue_ahead        = bid_size if pending_side == "BUY" else ask_size
                last_size_at_price = queue_ahead
                n_orders += 1
            elif dut.order_cancel.value == 1 and pending and \
                    int(dut.order_id.value) == pending_id:
                await send_ack(dut, pending_id, ACK_CANCELLED)
                pending = False
                n_cancels += 1

        if not pending:
            continue

        tol = 0
        trade_through = False
        if last_trade is not None:
            if pending_side == "BUY"  and last_trade <= pending_price + tol:
                trade_through = True
            elif pending_side == "SELL" and last_trade >= pending_price - tol:
                trade_through = True

        at_our_price = ((pending_side == "BUY"  and bid == pending_price) or
                        (pending_side == "SELL" and ask == pending_price))
        size_at_price = bid_size if pending_side == "BUY" else ask_size
        if at_our_price:
            if size_at_price < last_size_at_price:
                queue_ahead = max(0, queue_ahead - (last_size_at_price - size_at_price))
            last_size_at_price = size_at_price
        else:
            last_size_at_price = 0

        filled = trade_through and queue_ahead <= 0
        if trade_through and not filled:
            n_queue_misses += 1

        if filled:
            edge_per_share = (mid - pending_price) if pending_side == "BUY" else (pending_price - mid)
            edge_usd  = edge_per_share * TICK_SIZE_USD * ORDER_QTY
            rebate    = ORDER_QTY * MAKER_REBATE_PER_SHARE
            realized_pnl_usd += edge_usd + rebate
            signed_qty  = ORDER_QTY if pending_side == "BUY" else -ORDER_QTY
            position   += signed_qty
            cost_basis += signed_qty * pending_price * TICK_SIZE_USD
            await send_ack(dut, pending_id, ACK_FILLED, fill_qty=ORDER_QTY)
            pending = False
            n_fills += 1

    # Mark any residual position to the last known mid — an open position
    # that's never closed isn't free money, it's unrealized risk (same
    # treatment as nasdaq_mm_backtest.py's mark_to_mid_pnl).
    mark_to_mid_usd = (position * last_mid * TICK_SIZE_USD - cost_basis) if position != 0 else 0.0

    print(f"\n-- NASDAQ replay result ({csv_path.name}) --")
    print(f"  Rows replayed:   {len(rows):,}")
    print(f"  Orders placed:   {n_orders:,}")
    print(f"  Fills:           {n_fills:,}  ({n_fills/max(1,n_orders):.1%})")
    print(f"  Cancels (stale): {n_cancels:,}")
    print(f"  Queue misses:    {n_queue_misses:,}")
    print(f"  End position:    {position:+d}")
    print(f"  Realized P&L:    ${realized_pnl_usd:+.2f}")
    print(f"  Mark-to-mid:     ${mark_to_mid_usd:+.2f}")
    print(f"  Total P&L:       ${realized_pnl_usd + mark_to_mid_usd:+.2f}")

    assert n_orders > 0, "Strategy never placed an order — check book inputs/params"
