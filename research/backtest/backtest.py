#!/usr/bin/env python3
"""
backtest.py — Market-making strategy backtest using recorded bid/ask data.

Simulates the FPGA strategy (strategy.sv) on historical tick data:
  - Posts BUY at bid - QUOTE_OFFSET when position <= 0 and not pending
  - Posts SELL at ask + QUOTE_OFFSET when position >= 0 and not pending
  - Fills when price crosses the quoted level (same model as ack_sim_realistic)
  - Cancels (stale) when price moves > STALE_THRESH from quoted price
  - Tracks P&L, fill rate, position distribution, adverse selection

Usage:
  # Run with collected data:
  python backtest.py --symbol avaxusdt --date 20260321

  # Parameter sweep:
  python backtest.py --symbol linkusdt --sweep

  # All symbols for a date:
  python backtest.py --date 20260321 --all
"""

import argparse
import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent.parent / "data"

# ---------------------------------------------------------------------------
# Strategy parameters — mirror strategy.sv defaults
# ---------------------------------------------------------------------------

@dataclass
class StrategyParams:
    quote_offset:  int   = 2000    # ticks — how far inside bid/ask to quote
    stale_thresh:  int   = 2000    # ticks — cancel when price moves this far
    spread_max:    int   = 20000   # ticks — don't quote when spread is wider
    cooldown_cyc:  int   = 12_500_000  # ticks of cooldown (at 125MHz: 100ms)
    # In backtest time units (rows): approximate cooldown as N rows at ~10 rows/s
    cooldown_rows: int   = 1       # rows between orders (1 = no cooldown)
    order_qty:     int   = 100


# ---------------------------------------------------------------------------
# Fill model
# ---------------------------------------------------------------------------

class FillModel:
    """
    Realistic limit-order fill model.

    A BUY order at price P fills when best_ask <= P.
    A SELL order at price P fills when best_bid >= P.

    Orders are cancelled by:
      1. The strategy's stale detection (price moves > STALE_THRESH).
      2. A max hold time (fill_window_rows) — safety net.
    """

    def __init__(self, params: StrategyParams, fill_window_rows: int = 500):
        self.p               = params
        self.fill_window     = fill_window_rows  # rows before auto-cancel
        # Pending state
        self.pending         = False
        self.pending_side    = None   # "BUY" or "SELL"
        self.pending_price   = 0.0    # USD
        self.quoted_tick     = 0      # tick index when order was placed
        self.quoted_bid_tick = 0      # bid tick at time of order (for stale check)
        self.quoted_ask_tick = 0      # ask tick at time of order
        self.pending_row     = 0      # row index when order was placed
        self.cooldown_until  = -1     # row index when cooldown expires

    def _is_stale(self, bid_tick: int, ask_tick: int) -> bool:
        if not self.pending:
            return False
        if self.pending_side == "BUY":
            return abs(bid_tick - self.quoted_bid_tick) > self.p.stale_thresh
        else:
            return abs(ask_tick - self.quoted_ask_tick) > self.p.stale_thresh

    def step(self, row_idx: int,
             bid: float, ask: float,
             bid_tick: int, ask_tick: int,
             spread_tick: int,
             position: int) -> tuple[Optional[str], float]:
        """
        Process one tick.  Returns (event, fill_price_usd):
          ("FILLED", price)   — order filled this tick
          ("CANCELLED", 0)    — stale cancel or timeout
          ("NEW_BUY", price)  — new BUY order placed
          ("NEW_SELL", price) — new SELL order placed
          (None, 0)           — nothing happened
        """
        # ── Check existing pending order ─────────────────────────────────
        if self.pending:
            # Stale cancel
            if self._is_stale(bid_tick, ask_tick):
                self.pending = False
                return ("CANCELLED", 0.0)

            # Fill-window timeout
            if row_idx - self.pending_row > self.fill_window:
                self.pending = False
                return ("CANCELLED", 0.0)

            # Fill check
            if self.pending_side == "BUY"  and ask <= self.pending_price:
                self.pending = False
                self.cooldown_until = row_idx + self.p.cooldown_rows
                return ("FILLED", self.pending_price)
            if self.pending_side == "SELL" and bid >= self.pending_price:
                self.pending = False
                self.cooldown_until = row_idx + self.p.cooldown_rows
                return ("FILLED", self.pending_price)

            return (None, 0.0)   # waiting for fill

        # ── Try to place new order ────────────────────────────────────────
        if row_idx < self.cooldown_until:
            return (None, 0.0)   # in cooldown
        if spread_tick > self.p.spread_max:
            return (None, 0.0)   # spread too wide

        if position <= 0:        # asymmetric: BUY when flat or short
            price_usd = bid - self.p.quote_offset * (ask - bid) / max(1, spread_tick) \
                if spread_tick > 0 else bid
            # Simpler: quote_offset in price units directly via tick_size (set externally)
            # price_usd is set by caller via buy_price/sell_price
            pass

        # Return None here — caller uses get_quotes() for prices
        return (None, 0.0)

    def get_quotes(self, bid: float, ask: float, tick_size: float,
                   position: int) -> tuple[Optional[str], float]:
        """Return the order side and price the strategy would quote, or None."""
        buy_price  = bid - self.p.quote_offset * tick_size
        sell_price = ask + self.p.quote_offset * tick_size
        if position <= 0:
            return "BUY", buy_price
        if position >= 0:
            return "SELL", sell_price
        return None, 0.0


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    symbol:        str
    params:        StrategyParams
    tick_size:     float

    # Aggregates
    n_rows:        int   = 0
    n_fills:       int   = 0
    n_cancels:     int   = 0
    n_orders:      int   = 0
    total_pnl:     float = 0.0
    total_rebate:  float = 0.0

    # Per-fill tracking
    fill_edges:    list  = field(default_factory=list)  # pnl per fill excl rebate
    adverse_moves: list  = field(default_factory=list)  # mid move from fill to next cancel
    hold_times:    list  = field(default_factory=list)  # rows held before fill

    # Position
    max_long:      int   = 0
    max_short:     int   = 0

    MAKER_REBATE_BPS: float = 1.0   # 0.01% rebate

    def record_fill(self, side: str, fill_price: float, mid_at_fill: float,
                    qty: int, hold_rows: int) -> None:
        edge = (mid_at_fill - fill_price) if side == "BUY" else (fill_price - mid_at_fill)
        notional = fill_price * qty
        rebate   = notional * self.MAKER_REBATE_BPS / 10_000
        self.fill_edges.append(edge)
        self.hold_times.append(hold_rows)
        self.total_pnl    += edge + rebate
        self.total_rebate += rebate
        self.n_fills      += 1

    def fill_rate(self) -> float:
        return self.n_fills / max(1, self.n_orders)

    def avg_edge(self) -> float:
        return sum(self.fill_edges) / max(1, len(self.fill_edges))

    def pnl_per_hour(self, elapsed_s: float) -> float:
        return self.total_pnl / max(1, elapsed_s) * 3600

    def sharpe(self) -> float:
        if len(self.fill_edges) < 2:
            return float("nan")
        mean = sum(self.fill_edges) / len(self.fill_edges)
        var  = sum((x - mean) ** 2 for x in self.fill_edges) / len(self.fill_edges)
        return mean / math.sqrt(var) if var > 0 else float("nan")

    def pnl_per_day(self, elapsed_s: float) -> float:
        return self.total_pnl / max(1, elapsed_s) * 86400

    def summary(self, elapsed_s: float = 0.0) -> str:
        lines = [
            f"  Symbol:       {self.symbol.upper()}",
            f"  QUOTE_OFFSET: {self.params.quote_offset} ticks"
            f"  = ${self.params.quote_offset * self.tick_size:.4f}",
            f"  STALE_THRESH: {self.params.stale_thresh} ticks",
            f"  Rows:         {self.n_rows:,}",
            f"  Orders:       {self.n_orders:,}",
            f"  Fills:        {self.n_fills:,}  ({self.fill_rate():.1%})",
            f"  Cancels:      {self.n_cancels:,}",
            f"  Total P&L:    ${self.total_pnl:+.4f}",
            f"  Maker rebate: ${self.total_rebate:+.4f}",
            f"  Avg edge/fill:${self.avg_edge():+.4f}",
            f"  Sharpe:       {self.sharpe():.2f}",
            f"  Max long:     {self.max_long}",
            f"  Max short:    {self.max_short}",
        ]
        if elapsed_s > 0:
            lines.append(f"  P&L/hour:     ${self.pnl_per_hour(elapsed_s):+.4f}")
            lines.append(f"  P&L/day:      ${self.pnl_per_day(elapsed_s):+.4f}")
        return "\n".join(lines)


def run_backtest(
    symbol: str,
    tick_size: float,
    rows: list[tuple],       # (timestamp_ns, bid, ask)
    params: StrategyParams,
    fill_window_rows: int = 300,
    verbose: bool = False,
) -> BacktestResult:
    """
    Core backtest loop.  rows = list of (timestamp_ns, bid, ask).
    """
    result  = BacktestResult(symbol=symbol, params=params, tick_size=tick_size)
    result.n_rows = len(rows)

    if not rows:
        return result

    position     = 0
    pending      = False
    pending_side = None
    pending_price= 0.0
    pending_bid  = 0.0   # bid at time of order (for stale)
    pending_ask  = 0.0   # ask at time of order
    pending_row  = 0
    cooldown_until = -1
    order_qty    = params.order_qty

    elapsed_s = (rows[-1][0] - rows[0][0]) / 1e9 if len(rows) > 1 else 1.0

    for i, row in enumerate(rows):
        _, bid, ask, bar_low, bar_high = row
        mid         = (bid + ask) / 2
        spread      = ask - bid
        spread_tick = round(spread / tick_size) if spread > 0 else 0

        # ── Pending order logic ───────────────────────────────────────────
        if pending:
            # Stale check: price moved > STALE_THRESH ticks from when we quoted
            if pending_side == "BUY":
                stale = abs(bid - pending_bid) > params.stale_thresh * tick_size
            else:
                stale = abs(ask - pending_ask) > params.stale_thresh * tick_size

            if stale or (i - pending_row) > fill_window_rows:
                pending = False
                result.n_cancels += 1
                continue

            # Fill check: use bar_low/bar_high so kline bars work correctly.
            # Real tick data: bar_low=ask, bar_high=bid (same as ask/bid).
            # Kline data:     bar_low=bar low, bar_high=bar high.
            if pending_side == "BUY"  and bar_low <= pending_price:
                position     += order_qty
                result.record_fill("BUY",  pending_price, mid, order_qty, i - pending_row)
                pending       = False
                cooldown_until= i + params.cooldown_rows
                result.max_long  = max(result.max_long,  position)
                result.max_short = max(result.max_short, -position)
                if verbose:
                    print(f"  FILL BUY  row={i:6d}  bid={bid:.4f}"
                          f"  fill={pending_price:.4f}  pos={position:+d}")
                continue

            if pending_side == "SELL" and bar_high >= pending_price:
                position     -= order_qty
                result.record_fill("SELL", pending_price, mid, order_qty, i - pending_row)
                pending       = False
                cooldown_until= i + params.cooldown_rows
                result.max_long  = max(result.max_long,  position)
                result.max_short = max(result.max_short, -position)
                if verbose:
                    print(f"  FILL SELL row={i:6d}  ask={ask:.4f}"
                          f"  fill={pending_price:.4f}  pos={position:+d}")
                continue

            continue   # waiting for fill

        # ── Try to place new order ────────────────────────────────────────
        if i < cooldown_until:
            continue
        if spread_tick > params.spread_max:
            continue

        buy_price  = bid - params.quote_offset * tick_size
        sell_price = ask + params.quote_offset * tick_size

        if position <= 0:
            side, price = "BUY", buy_price
        else:
            side, price = "SELL", sell_price

        if price <= 0:
            continue

        pending       = True
        pending_side  = side
        pending_price = price
        pending_bid   = bid
        pending_ask   = ask
        pending_row   = i
        result.n_orders += 1

    result.elapsed_s = elapsed_s   # type: ignore[attr-defined]
    return result


# ---------------------------------------------------------------------------
# Data loader
# ---------------------------------------------------------------------------

SYMBOL_TICK_SIZES = {
    "avaxusdt":  0.01,
    "linkusdt":  0.001,
    "aaveusdt":  0.01,
    "injusdt":   0.001,
    "ethusdt":   0.01,
    "solusdt":   0.001,
    "bnbusdt":   0.01,
    "btcusdt":   0.01,
    # Add more as needed
}


def load_csv(path: Path, max_rows: int = 0) -> list[tuple]:
    """
    Load rows from a collector or kline CSV file.

    Returns list of (timestamp_ns, bid, ask, bar_low, bar_high) where:
      - bid/ask = quoting prices (close for klines, real bid/ask from collector)
      - bar_low/bar_high = fill check bounds (= bid/ask for real tick data;
        bar low/high for klines — price touched our limit if low<=P or high>=P)
    """
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bid = float(row["bid"])
                ask = float(row["ask"])
                # kline files have bar_low/bar_high; collector files use bid/ask
                low  = float(row.get("bar_low",  bid))
                high = float(row.get("bar_high", ask))
                rows.append((int(row["timestamp_ns"]), bid, ask, low, high))
            except (KeyError, ValueError):
                continue
            if max_rows and len(rows) >= max_rows:
                break
    return rows


def find_data_files(symbol: str, date: str = "") -> list[Path]:
    """Find CSV files for a symbol, optionally filtered by date."""
    pattern = f"{symbol}_*.csv" if not date else f"{symbol}_{date}.csv"
    return sorted(DATA_DIR.glob(pattern))


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------

def param_sweep(symbol: str, rows: list[tuple], tick_size: float,
                offsets: list[int] = None,
                stale_threshes: list[int] = None) -> list[BacktestResult]:
    """Grid search over QUOTE_OFFSET and STALE_THRESH."""
    if offsets is None:
        # Realistic range for market making: 1–20 ticks from mid.
        # 1s kline bar ranges are typically 1–20 ticks depending on symbol.
        offsets = [1, 2, 3, 5, 10, 15, 20]
    if stale_threshes is None:
        stale_threshes = [2, 5, 10, 20]

    results = []
    for qo in offsets:
        for st in stale_threshes:
            p = StrategyParams(quote_offset=qo, stale_thresh=st)
            r = run_backtest(symbol, tick_size, rows, p)
            results.append(r)

    return results


def print_sweep_table(results: list[BacktestResult]) -> None:
    print(f"\n{'QUOTE_OFF':>10} {'STALE':>8} {'FILLS':>7} {'FILL%':>7}"
          f" {'P&L':>10} {'P&L/DAY':>9} {'AVG_EDGE':>10} {'SHARPE':>8}")
    print("-" * 80)
    for r in sorted(results, key=lambda x: x.total_pnl, reverse=True):
        elapsed = getattr(r, "elapsed_s", 0.0)
        pnl_day = r.pnl_per_day(elapsed) if elapsed > 0 else 0.0
        print(
            f"{r.params.quote_offset:>10d}"
            f"{r.params.stale_thresh:>8d}"
            f"{r.n_fills:>7d}"
            f"{r.fill_rate():>7.1%}"
            f"  ${r.total_pnl:>+8.4f}"
            f"  ${pnl_day:>+7.4f}"
            f"  ${r.avg_edge():>+8.4f}"
            f"{r.sharpe():>8.2f}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Market-making backtest")
    parser.add_argument("--symbol",  default="avaxusdt", help="Symbol to backtest")
    parser.add_argument("--date",    default="",         help="Date YYYYMMDD (default: latest)")
    parser.add_argument("--file",    default="",         help="Direct CSV file path (overrides --symbol/--date)")
    parser.add_argument("--offset",  type=int, default=2000, help="QUOTE_OFFSET ticks")
    parser.add_argument("--stale",   type=int, default=2000, help="STALE_THRESH ticks")
    parser.add_argument("--sweep",   action="store_true",    help="Parameter sweep mode")
    parser.add_argument("--all",     action="store_true",    help="Run all 4 trading symbols")
    parser.add_argument("--verbose", action="store_true",    help="Print each fill")
    parser.add_argument("--max-rows",type=int, default=0,   help="Limit rows loaded")
    args = parser.parse_args()

    TRADING_SYMBOLS = ["avaxusdt", "linkusdt", "aaveusdt", "injusdt"]

    if args.file:
        # Direct file path mode — infer symbol from filename
        p = Path(args.file)
        sym = p.stem.split("_")[0].lower()
        tick = SYMBOL_TICK_SIZES.get(sym, 0.01)
        all_rows = load_csv(p, args.max_rows)
        if not all_rows:
            print(f"No rows loaded from {p}")
            return
        print(f"\n[{sym.upper()}]  {len(all_rows):,} ticks  tick_size=${tick}  ({p.name})")
        if args.sweep:
            results = param_sweep(sym, all_rows, tick)
            print_sweep_table(results)
        else:
            pr = StrategyParams(quote_offset=args.offset, stale_thresh=args.stale)
            r  = run_backtest(sym, tick, all_rows, pr, verbose=args.verbose)
            elapsed = (all_rows[-1][0] - all_rows[0][0]) / 1e9 if all_rows else 1
            print(r.summary(elapsed))
        return

    symbols = TRADING_SYMBOLS if args.all else [args.symbol]

    for sym in symbols:
        tick = SYMBOL_TICK_SIZES.get(sym, 0.01)

        # Find data files
        files = find_data_files(sym, args.date)
        if not files:
            print(f"\n[{sym.upper()}] No data found in {DATA_DIR}/")
            print(f"  Run: python data_collector.py --symbols {sym}")
            continue

        # Load all matching files
        all_rows = []
        for f in files:
            all_rows.extend(load_csv(f, args.max_rows))
        all_rows.sort(key=lambda r: r[0])   # sort by timestamp

        print(f"\n[{sym.upper()}]  {len(all_rows):,} ticks  tick_size=${tick}")

        if args.sweep:
            results = param_sweep(sym, all_rows, tick)
            print_sweep_table(results)
        else:
            p = StrategyParams(quote_offset=args.offset, stale_thresh=args.stale)
            r = run_backtest(sym, tick, all_rows, p, verbose=args.verbose)
            elapsed = (all_rows[-1][0] - all_rows[0][0]) / 1e9 if all_rows else 1
            print(r.summary(elapsed))


if __name__ == "__main__":
    main()
