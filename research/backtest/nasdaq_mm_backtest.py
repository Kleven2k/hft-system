#!/usr/bin/env python3
"""
nasdaq_mm_backtest.py — Market-making backtest on NASDAQ ITCH 5.0 data.

Simulates a market maker quoting at the best bid/ask for a single stock.
Uses a reconstructed order book from real or synthetic ITCH 5.0 messages.

Strategy (mirrors strategy.sv):
  - Quote BUY  at best_bid  (passive, hoping to be filled by aggressive seller)
  - Quote SELL at best_ask  (passive, hoping to be filled by aggressive buyer)
  - Cancel and re-quote when the book moves (stale detection)
  - One pending order at a time per side (same as FPGA OMS)

Fill model:
  BUY  fills when an execution drives the ask down to our bid level.
  SELL fills when an execution drives the bid up to our ask level.
  (More precisely: we fill when our quote is at or inside the new best bid/ask
   after an execution event.)

What you learn from this backtest:
  - Adverse selection: how often does price move against you after a fill?
  - Fill rate vs offset: tighter quotes fill more but with worse adverse selection
  - Spread capture: how much of the bid-ask spread do you actually keep?
  - ITCH message microstructure: what mix of adds/cancels/execs drives the book?

Usage:
  # Synthetic data (no download needed):
  python research/backtest/nasdaq_mm_backtest.py --synthetic

  # Sweep quote offsets on synthetic data:
  python research/backtest/nasdaq_mm_backtest.py --synthetic --sweep

  # Real ITCH data (after downloading):
  python research/backtest/nasdaq_mm_backtest.py --file path/to/ITCH50.gz --symbol AAPL

  # Real data with sweep:
  python research/backtest/nasdaq_mm_backtest.py --file path/to/ITCH50.gz --symbol AAPL --sweep
"""

import argparse
import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from nasdaq.order_book import OrderBook, BookTick
from nasdaq.synthetic import generate_ticks
from nasdaq.itch_parser import parse_file, SystemEvent


def load_booktick_csv(path: Path):
    """Load a pre-generated BookTick CSV (from the Rust itch_parser tool)."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bid = float(row["best_bid"]) if row["best_bid"] else None
            ask = float(row["best_ask"]) if row["best_ask"] else None
            lt  = float(row["last_trade"]) if row["last_trade"] else None
            yield BookTick(
                timestamp_ns = int(row["ts_ns"]),
                best_bid     = bid,
                best_ask     = ask,
                bid_size     = int(row["bid_size"]) if row["bid_size"] else 0,
                ask_size     = int(row["ask_size"]) if row["ask_size"] else 0,
                last_trade   = lt,
                event        = row["event"],
            )


# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------

@dataclass
class MMParams:
    # How many ticks inside the best price to quote
    # 0 = quote at best bid/ask (tightest, most fills, worst adverse selection)
    # 1 = one tick behind best (safer, fewer fills)
    quote_offset_ticks: int   = 0

    # Cancel if mid moves more than this many ticks from when we quoted
    stale_ticks:        int   = 3

    tick_size:          float = 0.01   # $0.01 for stocks

    # Order size (shares)
    order_qty:          int   = 100

    # Max absolute position (shares) — stop adding to a losing side once hit
    max_inventory:      int   = 500

    # Maker rebate — US exchanges pay ~0.2 cents/share for adding liquidity
    maker_rebate_per_share: float = 0.002   # $0.002/share

    # Taker fee (for modelling adverse selection cost)
    taker_fee_per_share: float = 0.003

    def quote_offset_usd(self) -> float:
        return self.quote_offset_ticks * self.tick_size


def median_mid_price(ticks, sample_size: int = 2000) -> Optional[float]:
    """
    Robust price estimate for sizing orders — the first tick(s) in a
    reconstructed book can be stale/partial-book garbage (e.g. a resting
    order at $1.01 on a $170 stock, left over before the book has enough
    adds to reflect the real market), so take the median of a sample
    rather than trusting a single early value.
    """
    mids = []
    for t in ticks:
        if t.best_bid and t.best_ask:
            mids.append((t.best_bid + t.best_ask) / 2.0)
        if len(mids) >= sample_size:
            break
    if not mids:
        return None
    mids.sort()
    return mids[len(mids) // 2]


def qty_for_notional(notional_usd: float, price: float, max_inventory_mult: int = 5) -> tuple[int, int]:
    """
    Derive (order_qty, max_inventory) in shares from a target notional per
    order, given the stock's current price — so AAPL/MSFT/AMD etc. are
    compared on equal capital-per-trade footing instead of a flat share
    count (100 shares of a $400 stock is 2x the notional of a $200 stock).
    """
    order_qty = max(1, round(notional_usd / price))
    return order_qty, order_qty * max_inventory_mult


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class MMResult:
    symbol:     str
    params:     MMParams

    n_ticks:    int   = 0
    n_orders:   int   = 0
    n_fills:    int   = 0
    n_cancels:  int   = 0
    queue_misses: int = 0   # trades printed through our price but queue hadn't cleared

    total_pnl:    float = 0.0
    total_rebate: float = 0.0
    mark_to_mid_pnl: float = 0.0   # unrealized P&L on any position left open at end of run

    # Position (shares; + = long, - = short) and running cost basis (USD)
    position:     int   = 0
    cost_basis:   float = 0.0
    max_long:     int   = 0
    max_short:    int   = 0

    # Per-fill tracking
    fill_edges:       list = field(default_factory=list)   # mid_at_fill - fill_price (BUY)
    adverse_moves:    list = field(default_factory=list)   # mid move 10 ticks after fill
    hold_times_ns:    list = field(default_factory=list)   # ns from order to fill

    # Book stats
    spread_bps_samples: list = field(default_factory=list)
    elapsed_ns:   int = 0

    # ITCH message type counts
    msg_counts: dict = field(default_factory=dict)

    def fill_rate(self) -> float:
        return self.n_fills / max(1, self.n_orders)

    def total_pnl_marked(self) -> float:
        """Realized P&L plus mark-to-mid on any residual open position."""
        return self.total_pnl + self.mark_to_mid_pnl

    def avg_edge(self) -> float:
        return sum(self.fill_edges) / max(1, len(self.fill_edges))

    def avg_spread_bps(self) -> float:
        return sum(self.spread_bps_samples) / max(1, len(self.spread_bps_samples))

    def adverse_selection_rate(self) -> float:
        """Fraction of fills where price moved against us in the next 10 events."""
        if not self.adverse_moves:
            return float("nan")
        bad = sum(1 for m in self.adverse_moves if m < 0)
        return bad / len(self.adverse_moves)

    def sharpe(self) -> float:
        if len(self.fill_edges) < 2:
            return float("nan")
        mean = sum(self.fill_edges) / len(self.fill_edges)
        var  = sum((x - mean) ** 2 for x in self.fill_edges) / len(self.fill_edges)
        return mean / math.sqrt(var) if var > 0 else float("nan")

    def elapsed_s(self) -> float:
        return self.elapsed_ns / 1e9

    def pnl_per_hour(self) -> float:
        return self.total_pnl_marked() / max(1, self.elapsed_s()) * 3600

    def summary(self) -> str:
        lines = [
            f"  Symbol:            {self.symbol}",
            f"  Quote offset:      {self.params.quote_offset_ticks} ticks"
            f" (${self.params.quote_offset_usd():.3f})",
            f"  Stale threshold:   {self.params.stale_ticks} ticks",
            f"  Order qty:         {self.params.order_qty} shares",
            f"  ---",
            f"  Book ticks:        {self.n_ticks:,}",
            f"  Elapsed:           {self.elapsed_s():.1f} s",
            f"  Avg spread:        {self.avg_spread_bps():.1f} bps",
            f"  ---",
            f"  Orders placed:     {self.n_orders:,}",
            f"  Fills:             {self.n_fills:,}  ({self.fill_rate():.1%})",
            f"  Cancels (stale):   {self.n_cancels:,}",
            f"  Queue misses:      {self.queue_misses:,}  (traded through our price, queue not cleared)",
            f"  ---",
            f"  Realized P&L:      ${self.total_pnl:+.4f}",
            f"  Mark-to-mid (open):${self.mark_to_mid_pnl:+.4f}",
            f"  Total P&L:         ${self.total_pnl_marked():+.4f}",
            f"  Maker rebate:      ${self.total_rebate:+.4f}",
            f"  Avg edge/fill:     ${self.avg_edge():+.5f}",
            f"  P&L/hour:          ${self.pnl_per_hour():+.4f}",
            f"  Sharpe:            {self.sharpe():.2f}",
            f"  Adverse sel. rate: {self.adverse_selection_rate():.1%}",
            f"  Max long:          {self.max_long}  Max short: {self.max_short}",
            f"  End position:      {self.position:+d}",
        ]
        if self.msg_counts:
            lines.append("  ---")
            lines.append("  ITCH message mix:")
            for k, v in sorted(self.msg_counts.items(), key=lambda x: -x[1]):
                lines.append(f"    {k:10s}: {v:,}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest(
    symbol:    str,
    messages,           # iterable of ITCH message objects
    params:    MMParams,
    verbose:   bool = False,
) -> MMResult:
    result = MMResult(symbol=symbol, params=params)
    book   = OrderBook(symbol)

    # Pending order state (one per side for simplicity)
    pending_side:  str   = ""      # "BUY" or "SELL"
    pending_price: float = 0.0
    pending_mid:   float = 0.0     # mid when we quoted (for edge calc)
    pending_ts:    int   = 0
    pending        = False
    cooldown_until_tick = -1       # tick index when cooldown expires

    # Circular buffer of recent mids for adverse selection measurement
    recent_mids: list[float] = []
    LOOKAHEAD = 10   # ticks to look ahead for adverse selection

    start_ts: int = 0
    first     = True

    for msg in messages:
        # Count message types
        mtype = type(msg).__name__
        result.msg_counts[mtype] = result.msg_counts.get(mtype, 0) + 1

        if isinstance(msg, SystemEvent):
            continue

        tick = book.apply(msg)
        if tick is None:
            continue

        if first and tick.best_bid and tick.best_ask:
            start_ts = tick.timestamp_ns
            first = False

        result.n_ticks += 1
        result.elapsed_ns = tick.timestamp_ns - start_ts

        # Track spread distribution
        spd = book.spread_bps()
        if spd:
            result.spread_bps_samples.append(spd)

        mid = book.mid()
        if mid is None:
            continue

        # Maintain rolling mid window for adverse selection
        recent_mids.append(mid)
        if len(recent_mids) > LOOKAHEAD + 1:
            recent_mids.pop(0)

        # ── Pending order logic ───────────────────────────────────────
        if pending:
            # Stale check: mid moved too far from when we quoted
            stale = abs(mid - pending_mid) > params.stale_ticks * params.tick_size
            if stale:
                pending = False
                result.n_cancels += 1
                continue

            # Fill check using last_trade price:
            # BUY  at P fills when a trade executes at price <= P
            #      (aggressive seller hit our bid or better)
            # SELL at P fills when a trade executes at price >= P
            #      (aggressive buyer hit our ask or better)
            tol = params.tick_size * 0.5   # floating point tolerance
            filled = False
            if (pending_side == "BUY"
                    and tick.event in ("EXEC",)
                    and tick.last_trade is not None
                    and tick.last_trade <= pending_price + tol):
                filled = True
            elif (pending_side == "SELL"
                    and tick.event in ("EXEC",)
                    and tick.last_trade is not None
                    and tick.last_trade >= pending_price - tol):
                filled = True

            if filled:
                # Edge = how far inside mid we filled
                if pending_side == "BUY":
                    edge = mid - pending_price
                else:
                    edge = pending_price - mid

                rebate = params.order_qty * params.maker_rebate_per_share
                result.fill_edges.append(edge * params.order_qty)
                result.total_pnl    += edge * params.order_qty + rebate
                result.total_rebate += rebate
                result.n_fills      += 1
                result.hold_times_ns.append(tick.timestamp_ns - pending_ts)

                signed_qty = params.order_qty if pending_side == "BUY" else -params.order_qty
                result.position   += signed_qty
                result.cost_basis += signed_qty * pending_price
                result.max_long   = max(result.max_long,  result.position)
                result.max_short  = max(result.max_short, -result.position)

                # Record adverse selection: mid movement LOOKAHEAD ticks later
                # (filled at index i, check recent_mids[-1] vs pending_mid)
                future_mid = recent_mids[-1] if recent_mids else mid
                if pending_side == "BUY":
                    adv = future_mid - pending_price   # positive = good (price went up)
                else:
                    adv = pending_price - future_mid
                result.adverse_moves.append(adv)

                pending = False
                cooldown_until_tick = result.n_ticks + 5   # 5-tick cooldown

                if verbose:
                    print(f"  FILL {pending_side:4s}  price=${pending_price:.4f}"
                          f"  mid=${mid:.4f}  edge=${edge*params.order_qty:+.4f}"
                          f"  pos={result.position:+d}  ts={tick.timestamp_ns}")
            continue

        # ── Place new order ───────────────────────────────────────────
        if result.n_ticks < cooldown_until_tick:
            continue
        if tick.best_bid is None or tick.best_ask is None:
            continue

        # Quote at best bid/ask minus/plus offset
        offset = params.quote_offset_usd()
        buy_price  = tick.best_bid  - offset
        sell_price = tick.best_ask  + offset

        # Quote on whichever side reduces inventory (mirrors strategy.sv):
        # flat or short -> BUY, flat or long -> SELL. Skip the side entirely
        # once max_inventory is hit, rather than accumulating unboundedly.
        side, price = None, 0.0
        if result.position <= 0 and result.position > -params.max_inventory:
            side, price = "BUY", buy_price
        elif result.position >= 0 and result.position < params.max_inventory:
            side, price = "SELL", sell_price

        if side is None or price <= 0:
            continue

        pending       = True
        pending_side  = side
        pending_price = price
        pending_mid   = mid
        pending_ts    = tick.timestamp_ns
        result.n_orders += 1

    # Mark any residual position to the last known mid — an open position
    # that's never closed isn't free money, it's unrealized risk.
    if result.position != 0 and recent_mids:
        result.mark_to_mid_pnl = result.position * recent_mids[-1] - result.cost_basis

    return result


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------

def run_backtest_ticks(
    symbol:    str,
    ticks,              # iterable of BookTick objects (from CSV or order book)
    params:    MMParams,
    verbose:   bool = False,
) -> MMResult:
    """Same as run_backtest but takes BookTick objects directly (skips order book)."""
    result = MMResult(symbol=symbol, params=params)

    pending_side:  str   = ""
    pending_price: float = 0.0
    pending_mid:   float = 0.0
    pending_ts:    int   = 0
    pending        = False
    cooldown_until_tick = -1

    # Queue-position tracking (FIFO price-time priority): when we join a
    # price level we're added to the BACK of the queue, behind whatever
    # size is already resting there. We can only fill once that much size
    # has left the queue (via executions or cancels ahead of us) AND a
    # further execution occurs at our price — being first to quote a level
    # is not the same as being first in line to fill.
    queue_ahead:        float = 0.0
    last_size_at_price: float = 0.0   # last observed size at pending_price

    recent_mids: list[float] = []
    LOOKAHEAD = 10

    start_ts: int = 0
    first     = True

    for tick in ticks:
        if tick.best_bid is None or tick.best_ask is None:
            continue

        mid = (tick.best_bid + tick.best_ask) / 2.0

        if first:
            start_ts = tick.timestamp_ns
            first = False

        result.n_ticks += 1
        result.elapsed_ns = tick.timestamp_ns - start_ts

        spd = (tick.best_ask - tick.best_bid) / mid * 10_000 if mid > 0 else None
        if spd:
            result.spread_bps_samples.append(spd)

        recent_mids.append(mid)
        if len(recent_mids) > LOOKAHEAD + 1:
            recent_mids.pop(0)

        if pending:
            stale = abs(mid - pending_mid) > params.stale_ticks * params.tick_size
            if stale:
                pending = False
                result.n_cancels += 1
                continue

            tol = params.tick_size * 0.5
            trade_through = False
            if (pending_side == "BUY"
                    and tick.event in ("EXEC",)
                    and tick.last_trade is not None
                    and tick.last_trade <= pending_price + tol):
                trade_through = True
            elif (pending_side == "SELL"
                    and tick.event in ("EXEC",)
                    and tick.last_trade is not None
                    and tick.last_trade >= pending_price - tol):
                trade_through = True

            # Track how much of the queue ahead of us has left, using the
            # observed size at our exact price level (only meaningful while
            # our price is still the best bid/ask — see run docstring above
            # for why this only models at-touch quoting, i.e. offset=0).
            size_at_price = (tick.bid_size if pending_side == "BUY" else tick.ask_size)
            at_our_price  = ((pending_side == "BUY"  and tick.best_bid == pending_price) or
                             (pending_side == "SELL" and tick.best_ask == pending_price))
            if at_our_price:
                if size_at_price < last_size_at_price:
                    queue_ahead = max(0.0, queue_ahead - (last_size_at_price - size_at_price))
                last_size_at_price = size_at_price
            else:
                # Price level no longer visible as best — can't observe our
                # queue directly; assume no progress (conservative).
                last_size_at_price = 0.0

            filled = trade_through and queue_ahead <= 0

            if trade_through and not filled:
                result.queue_misses += 1

            if filled:
                edge = mid - pending_price if pending_side == "BUY" else pending_price - mid
                rebate = params.order_qty * params.maker_rebate_per_share
                result.fill_edges.append(edge * params.order_qty)
                result.total_pnl    += edge * params.order_qty + rebate
                result.total_rebate += rebate
                result.n_fills      += 1
                result.hold_times_ns.append(tick.timestamp_ns - pending_ts)
                future_mid = recent_mids[-1] if recent_mids else mid
                adv = future_mid - pending_price if pending_side == "BUY" else pending_price - future_mid
                result.adverse_moves.append(adv)

                signed_qty = params.order_qty if pending_side == "BUY" else -params.order_qty
                result.position   += signed_qty
                result.cost_basis += signed_qty * pending_price
                result.max_long   = max(result.max_long,  result.position)
                result.max_short  = max(result.max_short, -result.position)

                pending = False
                cooldown_until_tick = result.n_ticks + 5
                if verbose:
                    print(f"  FILL {pending_side:4s}  price=${pending_price:.4f}"
                          f"  mid=${mid:.4f}  edge=${edge*params.order_qty:+.4f}"
                          f"  pos={result.position:+d}")
            continue

        if result.n_ticks < cooldown_until_tick:
            continue

        offset = params.quote_offset_usd()
        buy_price  = tick.best_bid  - offset
        sell_price = tick.best_ask  + offset

        # Quote on whichever side reduces inventory (mirrors strategy.sv):
        # flat or short -> BUY, flat or long -> SELL. Skip the side entirely
        # once max_inventory is hit, rather than accumulating unboundedly.
        side, price = None, 0.0
        if result.position <= 0 and result.position > -params.max_inventory:
            side, price = "BUY", buy_price
        elif result.position >= 0 and result.position < params.max_inventory:
            side, price = "SELL", sell_price

        if side is None or price <= 0:
            continue

        pending       = True
        pending_side  = side
        pending_price = price
        pending_mid   = mid
        pending_ts    = tick.timestamp_ns
        result.n_orders += 1

        # We join the BACK of the queue at this price -- everything already
        # resting there (bid_size/ask_size right now) is ahead of us.
        queue_ahead        = tick.bid_size if side == "BUY" else tick.ask_size
        last_size_at_price = queue_ahead

    # Mark any residual position to the last known mid — an open position
    # that's never closed isn't free money, it's unrealized risk.
    if result.position != 0 and recent_mids:
        result.mark_to_mid_pnl = result.position * recent_mids[-1] - result.cost_basis

    return result


def sweep_ticks(symbol: str, ticks_factory, tick_size: float,
                order_qty: int = 100, max_inventory: int = 500) -> None:
    """Sweep using pre-parsed BookTick objects (fast path)."""
    offsets        = [0, 1, 2, 3, 5]
    stale_threshes = [2, 3, 5, 10]

    print(f"\n{'OFFSET':>8} {'STALE':>7} {'FILLS':>7} {'FILL%':>7}"
          f" {'P&L':>10} {'P&L/HR':>9} {'AVG_EDGE':>10} {'ADV_SEL':>9} {'SHARPE':>8}")
    print("-" * 85)

    results = []
    for qo in offsets:
        for st in stale_threshes:
            p = MMParams(quote_offset_ticks=qo, stale_ticks=st, tick_size=tick_size,
                         order_qty=order_qty, max_inventory=max_inventory)
            r = run_backtest_ticks(symbol, ticks_factory(), p)
            results.append(r)

    for r in sorted(results, key=lambda x: x.total_pnl_marked(), reverse=True):
        p = r.params
        print(
            f"{p.quote_offset_ticks:>8d}"
            f"{p.stale_ticks:>7d}"
            f"{r.n_fills:>7d}"
            f"{r.fill_rate():>7.1%}"
            f"  ${r.total_pnl_marked():>+8.4f}"
            f"  ${r.pnl_per_hour():>+7.2f}"
            f"  ${r.avg_edge():>+8.5f}"
            f"  {r.adverse_selection_rate():>8.1%}"
            f"  {r.sharpe():>7.2f}"
        )


def sweep(symbol: str, messages_factory, tick_size: float,
          order_qty: int = 100, max_inventory: int = 500) -> None:
    offsets      = [0, 1, 2, 3, 5]
    stale_threshes = [2, 3, 5, 10]

    print(f"\n{'OFFSET':>8} {'STALE':>7} {'FILLS':>7} {'FILL%':>7}"
          f" {'P&L':>10} {'P&L/HR':>9} {'AVG_EDGE':>10} {'ADV_SEL':>9} {'SHARPE':>8}")
    print("-" * 85)

    results = []
    for qo in offsets:
        for st in stale_threshes:
            p = MMParams(quote_offset_ticks=qo, stale_ticks=st, tick_size=tick_size,
                         order_qty=order_qty, max_inventory=max_inventory)
            r = run_backtest(symbol, messages_factory(), p)
            results.append(r)

    for r in sorted(results, key=lambda x: x.total_pnl_marked(), reverse=True):
        p = r.params
        print(
            f"{p.quote_offset_ticks:>8d}"
            f"{p.stale_ticks:>7d}"
            f"{r.n_fills:>7d}"
            f"{r.fill_rate():>7.1%}"
            f"  ${r.total_pnl_marked():>+8.4f}"
            f"  ${r.pnl_per_hour():>+7.2f}"
            f"  ${r.avg_edge():>+8.5f}"
            f"  {r.adverse_selection_rate():>8.1%}"
            f"  {r.sharpe():>7.2f}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="NASDAQ ITCH market-making backtest")
    parser.add_argument("--file",        default="",        help="ITCH .gz file path")
    parser.add_argument("--symbol",      default="AAPL",    help="Ticker symbol")
    parser.add_argument("--offset",      type=int, default=0,
                        help="Quote offset in ticks from best bid/ask (default 0)")
    parser.add_argument("--stale",       type=int, default=3,
                        help="Cancel when mid moves this many ticks (default 3)")
    parser.add_argument("--tick-size",   type=float, default=0.01,
                        help="Tick size in dollars (default 0.01)")
    parser.add_argument("--qty",         type=int, default=100,
                        help="Order size in shares (default 100)")
    parser.add_argument("--notional",    type=float, default=0.0,
                        help="Target notional per order in USD (order_qty derived from "
                             "price; makes P&L comparable across symbols at different "
                             "price levels). 0 = use --qty as a flat share count instead")
    parser.add_argument("--synthetic",   action="store_true",
                        help="Use synthetic data (no ITCH file needed)")
    parser.add_argument("--synth-price", type=float, default=185.0,
                        help="Synthetic start price (default 185.0 for AAPL-like)")
    parser.add_argument("--synth-events",type=int, default=100_000,
                        help="Number of synthetic events (default 100000)")
    parser.add_argument("--ticks-file",  default="",
                        help="Pre-parsed BookTick CSV from Rust itch_parser (fast path)")
    parser.add_argument("--sweep",       action="store_true",
                        help="Sweep offset × stale_thresh parameter grid")
    parser.add_argument("--verbose",     action="store_true",
                        help="Print each fill")
    args = parser.parse_args()

    tick_size = args.tick_size

    if args.synthetic:
        print(f"Using synthetic data: symbol={args.symbol}"
              f"  price=${args.synth_price:.2f}"
              f"  events={args.synth_events:,}")

        def make_messages():
            return generate_ticks(
                symbol      = args.symbol,
                start_price = args.synth_price,
                n_events    = args.synth_events,
                tick_size   = tick_size,
            )

        order_qty, max_inv = (qty_for_notional(args.notional, args.synth_price)
                              if args.notional else (args.qty, 500))
        if args.notional:
            print(f"  order_qty={order_qty} (${args.notional:.0f} notional)")

        if args.sweep:
            sweep(args.symbol, make_messages, tick_size, order_qty=order_qty, max_inventory=max_inv)
            return

        params = MMParams(
            quote_offset_ticks = args.offset,
            stale_ticks        = args.stale,
            tick_size          = tick_size,
            order_qty          = order_qty,
            max_inventory       = max_inv,
        )
        result = run_backtest(args.symbol, make_messages(), params, args.verbose)
        print(f"\n-- Result --")
        print(result.summary())
        return

    # Fast path: pre-parsed BookTick CSV from Rust tool
    if args.ticks_file:
        ticks_path = Path(args.ticks_file)
        if not ticks_path.exists():
            print(f"File not found: {ticks_path}")
            return
        print(f"Loading pre-parsed ticks from {ticks_path.name} ...")
        cached = list(load_booktick_csv(ticks_path))

        typical_price = median_mid_price(cached)
        order_qty, max_inv = ((qty_for_notional(args.notional, typical_price))
                              if args.notional and typical_price else (args.qty, 500))
        if args.notional:
            print(f"  order_qty={order_qty} (${args.notional:.0f} notional)")

        if args.sweep:
            print(f"  Loaded {len(cached):,} ticks. Running sweep ...")
            sweep_ticks(args.symbol, lambda: iter(cached), tick_size,
                       order_qty=order_qty, max_inventory=max_inv)
        else:
            params = MMParams(
                quote_offset_ticks = args.offset,
                stale_ticks        = args.stale,
                tick_size          = tick_size,
                order_qty          = order_qty,
                max_inventory       = max_inv,
            )
            result = run_backtest_ticks(args.symbol, iter(cached), params, args.verbose)
            print(f"\n-- Result --")
            print(result.summary())
        return

    # Real ITCH file
    if not args.file:
        print("Provide --file path/to/ITCH50.gz  or use --synthetic")
        print("\nTo download real NASDAQ data:")
        print("  python research/nasdaq/download.py --date 20240101 --out research/data/nasdaq/")
        return

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}")
        return

    print(f"Parsing {path.name}  symbol={args.symbol} ...")

    if args.notional:
        print("  Note: --notional isn't supported on the raw --file path (price isn't known "
              "until the order book runs) — use --ticks-file instead. Falling back to --qty.")

    if args.sweep:
        print(f"  Pre-loading {args.symbol} messages into memory (once) ...")
        cached = list(parse_file(path, symbol_filter=args.symbol))
        print(f"  Loaded {len(cached):,} messages. Running sweep ...")
        sweep(args.symbol, lambda: iter(cached), tick_size, order_qty=args.qty)
        return

    params = MMParams(
        quote_offset_ticks = args.offset,
        stale_ticks        = args.stale,
        tick_size          = tick_size,
        order_qty          = args.qty,
    )
    messages = parse_file(path, symbol_filter=args.symbol)
    result   = run_backtest(args.symbol, messages, params, args.verbose)
    print(f"\n-- Result --")
    print(result.summary())


if __name__ == "__main__":
    main()
