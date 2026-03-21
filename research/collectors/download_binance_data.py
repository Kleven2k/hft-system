#!/usr/bin/env python3
"""
download_binance_data.py — Download Binance historical data for backtesting.

Downloads 1-second kline (OHLCV) data from Binance's public REST API and
converts it to the same bid/ask CSV format used by data_collector.py.

Approximation: bid ≈ low, ask ≈ high for each 1s bar.
This overestimates fill frequency vs real tick data but gives a fast
first-pass estimate of which symbols and parameters have edge.

For higher fidelity, run data_collector.py for a few days to capture
real bid/ask ticks (bookTicker WebSocket).

Usage:
  pip install requests
  python download_binance_data.py --symbols avaxusdt linkusdt --days 7
  python download_binance_data.py --symbols avaxusdt --days 30  # ~1M rows/month
"""

import argparse
import csv
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("downloader")

DATA_DIR = Path(__file__).parent / "data"

BINANCE_REST = "https://api.binance.com/api/v3/klines"

# Binance returns up to 1000 rows per request.
# For 1s klines: 1000 rows = ~16.7 minutes.
ROWS_PER_REQUEST = 1000

DEFAULT_SYMBOLS = ["avaxusdt", "linkusdt", "aaveusdt", "injusdt"]


def fetch_klines(symbol: str, interval: str,
                 start_ms: int, end_ms: int) -> list[list]:
    """Fetch klines from Binance REST API.  Returns raw list of lists."""
    params = {
        "symbol":    symbol.upper(),
        "interval":  interval,
        "startTime": start_ms,
        "endTime":   end_ms,
        "limit":     ROWS_PER_REQUEST,
    }
    r = requests.get(BINANCE_REST, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def download_symbol(symbol: str, days: int, interval: str = "1s") -> Path:
    """
    Download `days` of kline data for symbol and write to CSV.

    Returns path of written file.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    end_dt   = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=days)

    date_str = start_dt.strftime("%Y%m%d")
    path     = DATA_DIR / f"{symbol}_{date_str}_klines{days}d.csv"

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp()   * 1000)

    log.info(f"[{symbol.upper():>10}]  {days}d of {interval} klines"
             f"  {start_dt.date()} → {end_dt.date()}")

    total_rows = 0
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ns", "bid", "ask", "mid", "spread", "bar_low", "bar_high"])

        cursor = start_ms
        while cursor < end_ms:
            try:
                klines = fetch_klines(symbol, interval, cursor, end_ms)
            except requests.HTTPError as e:
                log.error(f"HTTP error: {e} — retrying in 5 s")
                time.sleep(5)
                continue
            except Exception as e:
                log.error(f"Error: {e} — retrying in 5 s")
                time.sleep(5)
                continue

            if not klines:
                break

            for k in klines:
                # k = [open_time_ms, open, high, low, close, volume, close_time_ms, ...]
                open_time_ms = int(k[0])
                high  = float(k[2])
                low   = float(k[3])
                close = float(k[4])

                # For quoting: use close as mid estimate (bid ≈ ask ≈ close).
                # For fill simulation: store bar_low/bar_high separately.
                #   BUY  at P fills if bar_low  <= P  (price dipped to our level)
                #   SELL at P fills if bar_high >= P  (price rose to our level)
                bid    = close   # quote slightly below close
                ask    = close   # quote slightly above close
                mid    = close
                spread = 0.0     # not meaningful from klines

                ts_ns = open_time_ms * 1_000_000  # ms → ns
                writer.writerow([ts_ns, bid, ask,
                                  f"{mid:.8f}", f"{spread:.8f}",
                                  f"{low:.8f}", f"{high:.8f}"])
                total_rows += 1

            # Advance cursor past last returned bar
            last_time = int(klines[-1][0])
            cursor = last_time + 1

            # Rate limit: Binance allows ~1200 req/min on public endpoints
            time.sleep(0.1)

            if total_rows % 50_000 == 0 and total_rows > 0:
                log.info(f"  {total_rows:,} rows downloaded …")

    log.info(f"[{symbol.upper():>10}]  {total_rows:,} rows → {path.name}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Binance historical kline data for backtesting"
    )
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                        metavar="SYM")
    parser.add_argument("--days",    type=int, default=7,
                        help="Number of days to download (default: 7)")
    parser.add_argument("--interval", default="1s",
                        choices=["1s", "1m", "5m"],
                        help="Kline interval (default: 1s — highest fidelity)")
    args = parser.parse_args()

    log.info(f"Downloading {args.days}d of {args.interval} data for: "
             f"{', '.join(s.upper() for s in args.symbols)}")
    log.info("Note: bid=low, ask=high approximation — overestimates spreads")
    log.info("      Use data_collector.py for real bid/ask ticks")

    for sym in args.symbols:
        try:
            download_symbol(sym.lower(), args.days, args.interval)
        except Exception as e:
            log.error(f"[{sym.upper()}] Failed: {e}")

    log.info("Done.  Run: python backtest.py --symbol avaxusdt --sweep")


if __name__ == "__main__":
    main()
