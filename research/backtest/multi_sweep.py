#!/usr/bin/env python3
"""
multi_sweep.py — Run MM backtest across multiple symbols and dates.

Shows which symbol/date combinations are consistently profitable,
and finds the best parameters across the full dataset.

Usage:
  python research/backtest/multi_sweep.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from nasdaq_mm_backtest import MMParams, run_backtest_ticks, load_booktick_csv

DATA = Path("research/data/nasdaq")

# All available tick files
FILES = {
    ("AAPL", "2019-03-27"): DATA / "aapl_20190327_ticks.csv",
    ("AAPL", "2019-10-30"): DATA / "aapl_20191030_ticks.csv",
    ("AAPL", "2020-01-30"): DATA / "aapl_20200130_ticks.csv",
    ("MSFT", "2019-03-27"): DATA / "msft_20190327_ticks.csv",
    ("MSFT", "2019-10-30"): DATA / "msft_20191030_ticks.csv",
    ("MSFT", "2020-01-30"): DATA / "msft_20200130_ticks.csv",
    ("AMD",  "2019-03-27"): DATA / "amd_20190327_ticks.csv",
    ("AMD",  "2019-10-30"): DATA / "amd_20191030_ticks.csv",
    ("AMD",  "2020-01-30"): DATA / "amd_20200130_ticks.csv",
}

# Parameter grid
PARAMS = [
    MMParams(quote_offset_ticks=0, stale_ticks=2),
    MMParams(quote_offset_ticks=0, stale_ticks=3),
    MMParams(quote_offset_ticks=1, stale_ticks=2),
    MMParams(quote_offset_ticks=1, stale_ticks=3),
    MMParams(quote_offset_ticks=2, stale_ticks=2),
    MMParams(quote_offset_ticks=2, stale_ticks=3),
]

def main():
    results = []  # (symbol, date, params, result)

    for (symbol, date), path in sorted(FILES.items()):
        if not path.exists():
            print(f"  SKIP {symbol} {date} — file not found")
            continue
        ticks = list(load_booktick_csv(path))
        print(f"  {symbol} {date}: {len(ticks):,} ticks", end="", flush=True)
        for p in PARAMS:
            r = run_backtest_ticks(symbol, iter(ticks), p)
            results.append((symbol, date, p, r))
        print(f"  done")

    # ── Per symbol/date summary (best params) ───────────────────────────────
    print(f"\n{'SYMBOL':<6} {'DATE':<12} {'OFF':>4} {'STALE':>5} {'FILLS':>6}"
          f" {'P&L/HR':>9} {'ADV_SEL':>8} {'SHARPE':>7}")
    print("-" * 70)

    seen = set()
    for symbol, date, p, r in sorted(results,
            key=lambda x: x[3].pnl_per_hour(), reverse=True):
        key = (symbol, date)
        if key in seen:
            continue
        seen.add(key)
        print(f"{symbol:<6} {date:<12}"
              f" {p.quote_offset_ticks:>4} {p.stale_ticks:>5}"
              f" {r.n_fills:>6}"
              f"  ${r.pnl_per_hour():>+7.2f}"
              f"  {r.adverse_selection_rate():>7.1%}"
              f"  {r.sharpe():>6.2f}")

    # ── Best parameters averaged across all dates per symbol ────────────────
    print(f"\n-- Average P&L/hr per symbol (best params) --")
    print(f"{'SYMBOL':<6} {'OFF':>4} {'STALE':>5}  {'AVG_PNL/HR':>11}  {'DATES_PROFITABLE':>16}")
    print("-" * 50)

    for symbol in ["AAPL", "MSFT", "AMD"]:
        for p in PARAMS:
            sym_results = [r for s, d, pp, r in results
                           if s == symbol and pp.quote_offset_ticks == p.quote_offset_ticks
                           and pp.stale_ticks == p.stale_ticks]
            if not sym_results:
                continue
            avg_pnl = sum(r.pnl_per_hour() for r in sym_results) / len(sym_results)
            n_profitable = sum(1 for r in sym_results if r.pnl_per_hour() > 0)
            print(f"{symbol:<6} {p.quote_offset_ticks:>4} {p.stale_ticks:>5}"
                  f"  ${avg_pnl:>+9.2f}/hr"
                  f"  {n_profitable}/{len(sym_results)} dates")

    # ── Overall best ────────────────────────────────────────────────────────
    print(f"\n-- Overall best single result --")
    best = max(results, key=lambda x: x[3].pnl_per_hour())
    symbol, date, p, r = best
    print(f"  {symbol} {date}  offset={p.quote_offset_ticks} stale={p.stale_ticks}"
          f"  P&L/hr=${r.pnl_per_hour():+.2f}"
          f"  fills={r.n_fills}"
          f"  Sharpe={r.sharpe():.2f}"
          f"  adv_sel={r.adverse_selection_rate():.1%}")


if __name__ == "__main__":
    main()
