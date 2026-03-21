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
import math
from dataclasses import dataclass, field
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from nasdaq.order_book import OrderBook, BookTick
from nasdaq.synthetic import generate_ticks
from nasdaq.itch_parser import parse_file, SystemEvent


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

    # Maker rebate — US exchanges pay ~0.2 cents/share for adding liquidity
    maker_rebate_per_share: float = 0.002   # $0.002/share

    # Taker fee (for modelling adverse selection cost)
    taker_fee_per_share: float = 0.003

    def quote_offset_usd(self) -> float:
        return self.quote_offset_ticks * self.tick_size


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

    total_pnl:    float = 0.0
    total_rebate: float = 0.0

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
        return self.total_pnl / max(1, self.elapsed_s()) * 3600

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
            f"  ---",
            f"  Total P&L:         ${self.total_pnl:+.4f}",
            f"  Maker rebate:      ${self.total_rebate:+.4f}",
            f"  Avg edge/fill:     ${self.avg_edge():+.5f}",
            f"  P&L/hour:          ${self.pnl_per_hour():+.4f}",
            f"  Sharpe:            {self.sharpe():.2f}",
            f"  Adverse sel. rate: {self.adverse_selection_rate():.1%}",
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
                          f"  ts={tick.timestamp_ns}")
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

        # Alternate sides to stay flat (same as FPGA strategy)
        # Simple: always try to buy first (can be made more sophisticated)
        if result.n_fills % 2 == 0:
            side, price = "BUY", buy_price
        else:
            side, price = "SELL", sell_price

        if price <= 0:
            continue

        pending       = True
        pending_side  = side
        pending_price = price
        pending_mid   = mid
        pending_ts    = tick.timestamp_ns
        result.n_orders += 1

    return result


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------

def sweep(symbol: str, messages_factory, tick_size: float) -> None:
    offsets      = [0, 1, 2, 3, 5]
    stale_threshes = [2, 3, 5, 10]

    print(f"\n{'OFFSET':>8} {'STALE':>7} {'FILLS':>7} {'FILL%':>7}"
          f" {'P&L':>10} {'P&L/HR':>9} {'AVG_EDGE':>10} {'ADV_SEL':>9} {'SHARPE':>8}")
    print("-" * 85)

    results = []
    for qo in offsets:
        for st in stale_threshes:
            p = MMParams(quote_offset_ticks=qo, stale_ticks=st, tick_size=tick_size)
            r = run_backtest(symbol, messages_factory(), p)
            results.append(r)

    for r in sorted(results, key=lambda x: x.total_pnl, reverse=True):
        p = r.params
        print(
            f"{p.quote_offset_ticks:>8d}"
            f"{p.stale_ticks:>7d}"
            f"{r.n_fills:>7d}"
            f"{r.fill_rate():>7.1%}"
            f"  ${r.total_pnl:>+8.4f}"
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
    parser.add_argument("--synthetic",   action="store_true",
                        help="Use synthetic data (no ITCH file needed)")
    parser.add_argument("--synth-price", type=float, default=185.0,
                        help="Synthetic start price (default 185.0 for AAPL-like)")
    parser.add_argument("--synth-events",type=int, default=100_000,
                        help="Number of synthetic events (default 100000)")
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

        if args.sweep:
            sweep(args.symbol, make_messages, tick_size)
            return

        params = MMParams(
            quote_offset_ticks = args.offset,
            stale_ticks        = args.stale,
            tick_size          = tick_size,
            order_qty          = args.qty,
        )
        result = run_backtest(args.symbol, make_messages(), params, args.verbose)
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

    if args.sweep:
        def make_messages():
            return parse_file(path, symbol_filter=args.symbol)
        sweep(args.symbol, make_messages, tick_size)
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
