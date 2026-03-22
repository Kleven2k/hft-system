#!/usr/bin/env python3
"""
bars_backtest.py — Mean-reversion + momentum backtest on minute bar data.

Uses 2 years of Alpaca minute bars to test two strategies:

1. MEAN REVERSION — buy when price drops X% below short EMA, sell when recovered
   - Classic market-making adjacent: fade short-term moves
   - Profits when price oscillates around a mean

2. MOMENTUM — buy when short EMA crosses above long EMA, sell on cross below
   - Trend following: ride directional moves
   - Profits when price trends

Both strategies use realistic assumptions:
  - $0.005/share commission (typical retail)
  - 1-minute execution delay (conservative)
  - Position sizing: fixed $10k notional per trade

Usage:
  python research/backtest/bars_backtest.py
  python research/backtest/bars_backtest.py --symbol AAPL --strategy both
  python research/backtest/bars_backtest.py --symbol NVDA --strategy momentum
"""

import argparse
import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


DATA_DIR = Path("research/data/alpaca")

# ---------------------------------------------------------------------------
# Bar dataclass
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    timestamp: str
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    int
    vwap:      float


def load_bars(path: Path) -> list[Bar]:
    bars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                bars.append(Bar(
                    timestamp = row["timestamp"],
                    open      = float(row["open"]),
                    high      = float(row["high"]),
                    low       = float(row["low"]),
                    close     = float(row["close"]),
                    volume    = int(float(row["volume"])),
                    vwap      = float(row["vwap"]) if row["vwap"] else float(row["close"]),
                ))
            except (ValueError, KeyError):
                continue
    return bars


def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    symbol:   str
    strategy: str
    params:   dict

    trades:       int   = 0
    wins:         int   = 0
    total_pnl:    float = 0.0
    total_comm:   float = 0.0
    pnl_series:   list  = field(default_factory=list)  # cumulative P&L per trade

    def win_rate(self) -> float:
        return self.wins / max(1, self.trades)

    def avg_pnl(self) -> float:
        return self.total_pnl / max(1, self.trades)

    def sharpe(self) -> float:
        if len(self.pnl_series) < 2:
            return float("nan")
        mean = sum(self.pnl_series) / len(self.pnl_series)
        var  = sum((x - mean)**2 for x in self.pnl_series) / len(self.pnl_series)
        return (mean / math.sqrt(var)) * math.sqrt(252 * 390) if var > 0 else float("nan")

    def max_drawdown(self) -> float:
        if not self.pnl_series:
            return 0.0
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in self.pnl_series:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def summary(self) -> str:
        return (
            f"  Strategy:      {self.strategy}\n"
            f"  Params:        {self.params}\n"
            f"  Trades:        {self.trades}\n"
            f"  Win rate:      {self.win_rate():.1%}\n"
            f"  Total P&L:     ${self.total_pnl:+.2f}\n"
            f"  Avg P&L/trade: ${self.avg_pnl():+.4f}\n"
            f"  Commission:    ${self.total_comm:.2f}\n"
            f"  Sharpe:        {self.sharpe():.2f}\n"
            f"  Max drawdown:  ${self.max_drawdown():.2f}"
        )


# ---------------------------------------------------------------------------
# Strategy 1: Mean Reversion
# ---------------------------------------------------------------------------

def run_mean_reversion(
    symbol:     str,
    bars:       list[Bar],
    ema_period: int   = 20,
    threshold:  float = 0.005,   # 0.5% drop to enter
    hold_bars:  int   = 5,       # hold for up to N bars
    notional:   float = 10_000,
    commission: float = 0.005,   # per share
) -> BacktestResult:
    result = BacktestResult(symbol=symbol, strategy="mean_reversion",
                            params={"ema": ema_period, "thresh": threshold,
                                    "hold": hold_bars})
    closes = [b.close for b in bars]
    emas   = ema(closes, ema_period)

    in_trade    = False
    entry_price = 0.0
    entry_bar   = 0
    shares      = 0

    for i in range(ema_period, len(bars)):
        bar     = bars[i]
        ema_val = emas[i]

        if not in_trade:
            # Enter: close dropped below EMA by threshold
            if bar.close < ema_val * (1 - threshold):
                entry_price = bars[i+1].open if i+1 < len(bars) else bar.close
                shares      = int(notional / entry_price)
                if shares < 1:
                    continue
                in_trade  = True
                entry_bar = i

        else:
            # Exit: price recovered above EMA, or held too long
            exit_now = (bar.close >= ema_val) or (i - entry_bar >= hold_bars)
            if exit_now:
                exit_price = bars[i+1].open if i+1 < len(bars) else bar.close
                pnl  = (exit_price - entry_price) * shares
                comm = commission * shares * 2   # entry + exit
                net  = pnl - comm
                result.trades     += 1
                result.total_pnl  += net
                result.total_comm += comm
                result.pnl_series.append(net)
                if net > 0:
                    result.wins += 1
                in_trade = False

    return result


# ---------------------------------------------------------------------------
# Strategy 2: EMA Crossover Momentum
# ---------------------------------------------------------------------------

def run_momentum(
    symbol:      str,
    bars:        list[Bar],
    fast_period: int   = 9,
    slow_period: int   = 21,
    notional:    float = 10_000,
    commission:  float = 0.005,
) -> BacktestResult:
    result = BacktestResult(symbol=symbol, strategy="momentum",
                            params={"fast": fast_period, "slow": slow_period})
    closes    = [b.close for b in bars]
    fast_emas = ema(closes, fast_period)
    slow_emas = ema(closes, slow_period)

    in_trade    = False
    entry_price = 0.0
    shares      = 0
    direction   = 0   # 1=long, -1=short

    for i in range(slow_period + 1, len(bars)):
        fast_now  = fast_emas[i]
        fast_prev = fast_emas[i-1]
        slow_now  = slow_emas[i]
        slow_prev = slow_emas[i-1]

        cross_up   = fast_prev <= slow_prev and fast_now > slow_now
        cross_down = fast_prev >= slow_prev and fast_now < slow_now

        if not in_trade:
            if cross_up:
                entry_price = bars[i+1].open if i+1 < len(bars) else bars[i].close
                shares      = int(notional / entry_price)
                if shares < 1: continue
                in_trade  = True
                direction = 1
            elif cross_down:
                entry_price = bars[i+1].open if i+1 < len(bars) else bars[i].close
                shares      = int(notional / entry_price)
                if shares < 1: continue
                in_trade  = True
                direction = -1

        else:
            # Exit on opposite cross
            exit_now = (direction == 1 and cross_down) or (direction == -1 and cross_up)
            if exit_now:
                exit_price = bars[i+1].open if i+1 < len(bars) else bars[i].close
                pnl  = (exit_price - entry_price) * shares * direction
                comm = commission * shares * 2
                net  = pnl - comm
                result.trades     += 1
                result.total_pnl  += net
                result.total_comm += comm
                result.pnl_series.append(net)
                if net > 0:
                    result.wins += 1
                in_trade  = False
                # Immediately re-enter in new direction
                entry_price = exit_price
                shares      = int(notional / entry_price)
                direction   = 1 if cross_up else -1
                if shares >= 1:
                    in_trade = True

    return result


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------

def sweep_mean_reversion(symbol: str, bars: list[Bar]):
    print(f"\n-- Mean Reversion Sweep: {symbol} --")
    print(f"{'EMA':>5} {'THRESH':>7} {'HOLD':>5} {'TRADES':>7} {'WIN%':>6}"
          f" {'P&L':>10} {'SHARPE':>7} {'MAXDD':>8}")
    print("-" * 65)

    results = []
    for ep in [10, 20, 50]:
        for th in [0.003, 0.005, 0.008, 0.012]:
            for hold in [3, 5, 10]:
                r = run_mean_reversion(symbol, bars, ema_period=ep,
                                       threshold=th, hold_bars=hold)
                results.append(r)

    for r in sorted(results, key=lambda x: x.total_pnl, reverse=True)[:10]:
        p = r.params
        print(f"{p['ema']:>5} {p['thresh']:>7.3%} {p['hold']:>5}"
              f" {r.trades:>7} {r.win_rate():>6.1%}"
              f"  ${r.total_pnl:>+8.2f}"
              f"  {r.sharpe():>6.2f}"
              f"  ${r.max_drawdown():>6.2f}")


def sweep_momentum(symbol: str, bars: list[Bar]):
    print(f"\n-- Momentum Sweep: {symbol} --")
    print(f"{'FAST':>5} {'SLOW':>5} {'TRADES':>7} {'WIN%':>6}"
          f" {'P&L':>10} {'SHARPE':>7} {'MAXDD':>8}")
    print("-" * 55)

    results = []
    for fast in [5, 9, 12]:
        for slow in [20, 26, 50]:
            if fast >= slow:
                continue
            r = run_momentum(symbol, bars, fast_period=fast, slow_period=slow)
            results.append(r)

    for r in sorted(results, key=lambda x: x.total_pnl, reverse=True):
        p = r.params
        print(f"{p['fast']:>5} {p['slow']:>5}"
              f" {r.trades:>7} {r.win_rate():>6.1%}"
              f"  ${r.total_pnl:>+8.2f}"
              f"  {r.sharpe():>6.2f}"
              f"  ${r.max_drawdown():>6.2f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_latest(symbol: str) -> Optional[Path]:
    files = sorted(DATA_DIR.glob(f"{symbol.lower()}_bars_1Min_*.csv"), reverse=True)
    return files[0] if files else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",   default="ALL",
                        help="Symbol to test (default: all)")
    parser.add_argument("--strategy", default="both",
                        choices=["mean_reversion", "momentum", "both"])
    parser.add_argument("--sweep",    action="store_true",
                        help="Sweep parameters")
    args = parser.parse_args()

    symbols = ["AAPL", "MSFT", "AMD", "NVDA", "QQQ"] if args.symbol == "ALL" \
              else [args.symbol.upper()]

    for symbol in symbols:
        path = find_latest(symbol)
        if not path:
            print(f"No data for {symbol} — run alpaca_history.py first")
            continue

        bars = load_bars(path)
        print(f"\n{'='*60}")
        print(f"  {symbol}  —  {len(bars):,} bars  ({path.name})")
        print(f"{'='*60}")

        if args.sweep:
            if args.strategy in ("mean_reversion", "both"):
                sweep_mean_reversion(symbol, bars)
            if args.strategy in ("momentum", "both"):
                sweep_momentum(symbol, bars)
        else:
            if args.strategy in ("mean_reversion", "both"):
                r = run_mean_reversion(symbol, bars)
                print(f"\n-- Mean Reversion (defaults) --")
                print(r.summary())
            if args.strategy in ("momentum", "both"):
                r = run_momentum(symbol, bars)
                print(f"\n-- Momentum (defaults) --")
                print(r.summary())


if __name__ == "__main__":
    main()
