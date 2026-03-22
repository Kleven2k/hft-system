#!/usr/bin/env python3
"""
alpaca_history.py — Download recent historical bar data from Alpaca.

Downloads minute-level OHLCV bars for a list of symbols using the
Alpaca paper trading API keys (no live account needed).

Saves to research/data/alpaca/<symbol>_bars_<date>.csv

Usage:
  python research/collectors/alpaca_history.py
  python research/collectors/alpaca_history.py --symbols AAPL MSFT --days 30
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

try:
    import urllib.request
    import json
except ImportError:
    pass

DATA_DIR = Path(__file__).parent.parent.parent / "research" / "data" / "alpaca"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://data.alpaca.markets/v2"

SYMBOLS_DEFAULT = ["AAPL", "MSFT", "AMD", "NVDA", "QQQ"]


def fetch_bars(symbol: str, start: str, end: str, timeframe: str, key_id: str, secret: str):
    """Fetch bars from Alpaca REST API, handles pagination."""
    bars = []
    next_token = None

    while True:
        url = (f"{BASE_URL}/stocks/{symbol}/bars"
               f"?timeframe={timeframe}"
               f"&start={start}&end={end}"
               f"&limit=10000&adjustment=raw&feed=iex")
        if next_token:
            url += f"&page_token={next_token}"

        req = urllib.request.Request(url, headers={
            "APCA-API-KEY-ID":     key_id,
            "APCA-API-SECRET-KEY": secret,
        })
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"  Error fetching {symbol}: {e}")
            break

        batch = data.get("bars", [])
        bars.extend(batch)
        next_token = data.get("next_page_token")
        if not next_token:
            break
        print(f"  {symbol}: {len(bars)} bars, fetching more...")

    return bars


def save_bars(symbol: str, bars: list, timeframe: str):
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    path  = DATA_DIR / f"{symbol.lower()}_bars_{timeframe}_{today}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume", "vwap", "trades"])
        for b in bars:
            writer.writerow([
                b.get("t", ""),
                b.get("o", ""),
                b.get("h", ""),
                b.get("l", ""),
                b.get("c", ""),
                b.get("v", ""),
                b.get("vw", ""),
                b.get("n", ""),
            ])
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=SYMBOLS_DEFAULT)
    parser.add_argument("--days",    type=int, default=365,
                        help="Number of calendar days to look back (default 365)")
    parser.add_argument("--timeframe", default="1Min",
                        help="Bar timeframe: 1Min, 5Min, 1Hour, 1Day (default 1Min)")
    args = parser.parse_args()

    key_id = os.environ.get("ALPACA_KEY_ID", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key_id or not secret:
        print("Set ALPACA_KEY_ID and ALPACA_SECRET_KEY in .env")
        sys.exit(1)

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str   = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Downloading {args.timeframe} bars: {start_str} → {end_str}")
    print(f"Symbols: {args.symbols}")
    print()

    for symbol in args.symbols:
        print(f"  {symbol} ...", end=" ", flush=True)
        bars = fetch_bars(symbol, start_str, end_str, args.timeframe, key_id, secret)
        if not bars:
            print("no data (market closed or auth error)")
            continue
        path = save_bars(symbol, bars, args.timeframe)
        print(f"{len(bars):,} bars → {path.name}")


if __name__ == "__main__":
    main()
