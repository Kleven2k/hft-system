#!/usr/bin/env python3
"""
dex_arb_backtest.py — DEX/CEX arbitrage backtest using dex_monitor CSV data.

Reads the spread CSV produced by research/monitors/dex_monitor.py and simulates
realistic arbitrage execution with:

  - Minimum 2-second gap between trades (one per Avalanche block)
  - AMM price impact on the DEX leg (constant-product formula)
  - Gas cost per transaction (Avalanche C-Chain)
  - CEX taker fee + DEX swap fee
  - Parameter sweeps: fee threshold, trade size, gas cost

Fill model:
  When spread_pct > threshold AND cooldown has expired:
    BUY cheaper leg, SELL expensive leg simultaneously.
    Net profit = |spread_usd| - cex_fee - dex_fee - price_impact - gas

Usage:
  # Single run with defaults:
  python research/backtest/dex_arb_backtest.py

  # Sweep trade sizes and thresholds:
  python research/backtest/dex_arb_backtest.py --sweep

  # Use a specific CSV file:
  python research/backtest/dex_arb_backtest.py --file research/data/dex_cex_spread_20260321.csv

  # Time-of-day breakdown:
  python research/backtest/dex_arb_backtest.py --time-of-day
"""

import argparse
import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent.parent / "data"

# ---------------------------------------------------------------------------
# Fee / cost model
# ---------------------------------------------------------------------------

@dataclass
class ArbParams:
    # Fee threshold — only trade if spread exceeds this
    fee_threshold_pct: float = 0.40   # % (TJ 0.30% + Binance taker 0.10%)

    # Trade size in USD notional
    notional_usd: float = 1_000.0

    # Gas cost per round-trip (AVAX tx + Binance order)
    # Avalanche C-Chain: ~0.001–0.01 AVAX gas, AVAX ~$9 → ~$0.01–0.09
    gas_usd: float = 0.10

    # CEX taker fee (Binance standard)
    cex_fee_pct: float = 0.10         # %

    # DEX swap fee (Trader Joe V1)
    dex_fee_pct: float = 0.30         # %

    # AMM liquidity (approximate USDC reserve in the pool)
    # Trader Joe V1 WAVAX/USDC.e — rough estimate $500k USDC side
    pool_liquidity_usd: float = 500_000.0

    # Minimum seconds between trades (Avalanche block time)
    block_time_s: float = 2.0

    def round_trip_fee_pct(self) -> float:
        return self.cex_fee_pct + self.dex_fee_pct

    def price_impact_pct(self) -> float:
        """
        AMM price impact for a trade of size notional_usd against a pool
        with total liquidity pool_liquidity_usd (one side).

        Constant product: impact ≈ notional / (2 * liquidity) * 100
        This is the first-order approximation of x*y=k slippage.
        """
        return self.notional_usd / (2.0 * self.pool_liquidity_usd) * 100.0

    def total_cost_pct(self) -> float:
        """Total round-trip cost as % of notional."""
        return self.round_trip_fee_pct() + self.price_impact_pct()

    def breakeven_pct(self) -> float:
        """Spread required to break even (fees + impact + gas)."""
        gas_pct = self.gas_usd / self.notional_usd * 100.0
        return self.total_cost_pct() + gas_pct


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ArbResult:
    params:        ArbParams
    n_rows:        int   = 0
    n_trades:      int   = 0
    n_missed:      int   = 0   # above threshold but in cooldown
    total_pnl:     float = 0.0
    total_fees:    float = 0.0
    total_gas:     float = 0.0
    total_impact:  float = 0.0
    gross_pnl:     float = 0.0  # before costs

    pnl_per_trade: list  = field(default_factory=list)
    spreads_traded:list  = field(default_factory=list)
    timestamps:    list  = field(default_factory=list)  # ns of each trade

    elapsed_s:     float = 0.0

    # Time-of-day buckets (hour → list of pnl)
    hourly_pnl:    dict  = field(default_factory=dict)

    def record_trade(self, spread_pct: float, cex_mid: float,
                     ts_ns: int) -> None:
        p = self.params
        gross        = abs(spread_pct) / 100.0 * p.notional_usd
        fees         = p.round_trip_fee_pct() / 100.0 * p.notional_usd
        impact       = p.price_impact_pct()   / 100.0 * p.notional_usd
        net          = gross - fees - impact - p.gas_usd

        self.n_trades       += 1
        self.gross_pnl      += gross
        self.total_fees     += fees
        self.total_impact   += impact
        self.total_gas      += p.gas_usd
        self.total_pnl      += net
        self.pnl_per_trade.append(net)
        self.spreads_traded.append(abs(spread_pct))
        self.timestamps.append(ts_ns)

        hour = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).hour
        self.hourly_pnl.setdefault(hour, []).append(net)

    def fill_rate(self) -> float:
        opportunities = self.n_trades + self.n_missed
        return self.n_trades / max(1, opportunities)

    def avg_spread_traded(self) -> float:
        return sum(self.spreads_traded) / max(1, len(self.spreads_traded))

    def sharpe(self) -> float:
        if len(self.pnl_per_trade) < 2:
            return float("nan")
        mean = sum(self.pnl_per_trade) / len(self.pnl_per_trade)
        var  = sum((x - mean) ** 2 for x in self.pnl_per_trade) / len(self.pnl_per_trade)
        return mean / math.sqrt(var) if var > 0 else float("nan")

    def pnl_per_day(self) -> float:
        return self.total_pnl / max(1, self.elapsed_s) * 86400

    def trades_per_hour(self) -> float:
        return self.n_trades / max(1, self.elapsed_s) * 3600

    def summary(self) -> str:
        p = self.params
        lines = [
            f"  Notional:        ${p.notional_usd:,.0f}",
            f"  Fee threshold:   {p.fee_threshold_pct:.2f}%",
            f"  Breakeven:       {p.breakeven_pct():.3f}%"
            f"  (fees {p.round_trip_fee_pct():.2f}%"
            f" + impact {p.price_impact_pct():.3f}%"
            f" + gas ${p.gas_usd:.2f})",
            f"  Rows:            {self.n_rows:,}",
            f"  Elapsed:         {self.elapsed_s/3600:.1f} h",
            f"  Trades:          {self.n_trades}",
            f"  Missed (cooldown):{self.n_missed}",
            f"  Trades/hour:     {self.trades_per_hour():.1f}",
            f"  Avg spread:      {self.avg_spread_traded():.3f}%",
            f"  Gross P&L:       ${self.gross_pnl:+.4f}",
            f"  Total fees:      ${self.total_fees:.4f}",
            f"  Total impact:    ${self.total_impact:.4f}",
            f"  Total gas:       ${self.total_gas:.4f}",
            f"  Net P&L:         ${self.total_pnl:+.4f}",
            f"  Net P&L/day:     ${self.pnl_per_day():+.2f}",
            f"  Sharpe:          {self.sharpe():.2f}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core backtest loop
# ---------------------------------------------------------------------------

def run_backtest(rows: list[tuple], params: ArbParams) -> ArbResult:
    """
    rows: list of (timestamp_ns, cex_mid, dex_price, spread_pct)
    """
    result = ArbResult(params=params)
    result.n_rows = len(rows)

    if not rows:
        return result

    result.elapsed_s = (rows[-1][0] - rows[0][0]) / 1e9 if len(rows) > 1 else 1.0

    last_trade_ts = 0.0   # timestamp (seconds) of last trade

    for ts_ns, cex_mid, dex_price, spread_pct in rows:
        ts_s = ts_ns / 1e9

        if abs(spread_pct) <= params.fee_threshold_pct:
            continue   # below threshold — no opportunity

        # Opportunity exists — check cooldown
        if (ts_s - last_trade_ts) < params.block_time_s:
            result.n_missed += 1
            continue

        # Execute
        result.record_trade(spread_pct, cex_mid, ts_ns)
        last_trade_ts = ts_s

    return result


# ---------------------------------------------------------------------------
# Data loader
# ---------------------------------------------------------------------------

def load_csv(path: Path) -> list[tuple]:
    """Load dex_cex_spread CSV → list of (timestamp_ns, cex_mid, dex_price, spread_pct)."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append((
                    int(row["timestamp_ns"]),
                    float(row["cex_mid"]),
                    float(row["dex_price"]),
                    float(row["spread_pct"]),
                ))
            except (KeyError, ValueError):
                continue
    return rows


def find_csv_files(date: str = "") -> list[Path]:
    pattern = f"dex_cex_spread_{date}*.csv" if date else "dex_cex_spread_*.csv"
    return sorted(DATA_DIR.glob(pattern))


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------

def sweep(rows: list[tuple]) -> None:
    notionals   = [1_000, 5_000, 10_000, 25_000, 50_000]
    thresholds  = [0.35, 0.40, 0.45, 0.50, 0.55]

    print(f"\n{'NOTIONAL':>10}  {'THRESH':>7}  {'BRKEVEN':>8}  {'TRADES':>7}"
          f"  {'NET_PNL':>9}  {'PNL/DAY':>9}  {'SHARPE':>7}")
    print("-" * 75)

    results = []
    for n in notionals:
        for t in thresholds:
            p = ArbParams(notional_usd=n, fee_threshold_pct=t)
            r = run_backtest(rows, p)
            results.append(r)

    for r in sorted(results, key=lambda x: x.pnl_per_day(), reverse=True):
        p = r.params
        print(
            f"  ${p.notional_usd:>8,.0f}"
            f"  {p.fee_threshold_pct:>6.2f}%"
            f"  {p.breakeven_pct():>7.3f}%"
            f"  {r.n_trades:>7d}"
            f"  ${r.total_pnl:>+8.4f}"
            f"  ${r.pnl_per_day():>+8.2f}"
            f"  {r.sharpe():>7.2f}"
        )


# ---------------------------------------------------------------------------
# Time-of-day analysis
# ---------------------------------------------------------------------------

def time_of_day(rows: list[tuple], params: ArbParams) -> None:
    result = run_backtest(rows, params)

    print(f"\nTime-of-day breakdown (UTC)  notional=${params.notional_usd:,.0f}"
          f"  threshold={params.fee_threshold_pct:.2f}%")
    print(f"{'HOUR':>6}  {'TRADES':>7}  {'NET_PNL':>9}  {'AVG_SPREAD':>11}")
    print("-" * 45)

    for hour in sorted(result.hourly_pnl.keys()):
        pnls = result.hourly_pnl[hour]
        print(f"  {hour:02d}:00  {len(pnls):>7d}  ${sum(pnls):>+8.4f}"
              f"  {result.avg_spread_traded():>10.3f}%")

    if not result.hourly_pnl:
        print("  No trades executed.")


# ---------------------------------------------------------------------------
# Spread distribution
# ---------------------------------------------------------------------------

def spread_distribution(rows: list[tuple]) -> None:
    spreads = [abs(r[3]) for r in rows]
    if not spreads:
        print("No data.")
        return

    buckets = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.80, 1.00, 999]
    labels  = ["0–0.10%","0.10–0.20%","0.20–0.30%","0.30–0.40%",
               "0.40–0.50%","0.50–0.60%","0.60–0.80%","0.80–1.00%",">1.00%"]

    counts = [0] * len(labels)
    for s in spreads:
        for i in range(len(buckets) - 1):
            if buckets[i] <= s < buckets[i + 1]:
                counts[i] += 1
                break

    total = len(spreads)
    avg   = sum(spreads) / total
    mx    = max(spreads)

    print(f"\nSpread distribution  ({total:,} observations)"
          f"  avg={avg:.3f}%  max={mx:.3f}%")
    print(f"{'BUCKET':>12}  {'COUNT':>7}  {'PCT':>7}  {'BAR'}")
    print("-" * 55)
    for label, count in zip(labels, counts):
        pct = count / total * 100
        bar = "#" * int(pct / 2)
        marker = " <- fee threshold" if label == "0.40–0.50%" else ""
        print(f"  {label:>10}  {count:>7,}  {pct:>6.1f}%  {bar}{marker}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="DEX/CEX arbitrage backtest")
    parser.add_argument("--file",        default="",    help="Direct CSV path")
    parser.add_argument("--date",        default="",    help="Date YYYYMMDD")
    parser.add_argument("--notional",    type=float, default=1_000.0,
                        help="Trade size USD (default 1000)")
    parser.add_argument("--threshold",   type=float, default=0.40,
                        help="Fee threshold %% (default 0.40)")
    parser.add_argument("--gas",         type=float, default=0.10,
                        help="Gas cost per tx USD (default 0.10)")
    parser.add_argument("--liquidity",   type=float, default=500_000.0,
                        help="Pool liquidity USD (default 500000)")
    parser.add_argument("--sweep",       action="store_true",
                        help="Sweep notional × threshold grid")
    parser.add_argument("--time-of-day", action="store_true",
                        help="Breakdown P&L by hour of day")
    parser.add_argument("--distribution",action="store_true",
                        help="Show spread distribution histogram")
    args = parser.parse_args()

    # Load data
    if args.file:
        files = [Path(args.file)]
    else:
        files = find_csv_files(args.date)

    if not files:
        print(f"No dex_cex_spread CSV files found in {DATA_DIR}/")
        print("Run: python research/monitors/dex_monitor.py")
        return

    rows = []
    for f in files:
        loaded = load_csv(f)
        rows.extend(loaded)
        print(f"Loaded {len(loaded):,} rows from {f.name}")

    rows.sort(key=lambda r: r[0])
    elapsed_h = (rows[-1][0] - rows[0][0]) / 1e9 / 3600 if len(rows) > 1 else 0
    print(f"Total: {len(rows):,} rows  ({elapsed_h:.1f} hours of data)\n")

    params = ArbParams(
        notional_usd      = args.notional,
        fee_threshold_pct = args.threshold,
        gas_usd           = args.gas,
        pool_liquidity_usd= args.liquidity,
    )

    if args.distribution:
        spread_distribution(rows)

    if args.sweep:
        sweep(rows)
        return

    if args.time_of_day:
        time_of_day(rows, params)
        return

    # Default: single run + distribution
    spread_distribution(rows)
    result = run_backtest(rows, params)
    print(f"\n-- Single run --------------------------------------")
    print(result.summary())


if __name__ == "__main__":
    main()
