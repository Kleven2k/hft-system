#!/usr/bin/env python3
"""
data_collector.py — Records live Binance best bid/ask to CSV files.

Run this continuously alongside (or instead of) feed_bridge to build
a historical dataset for backtesting.  Each symbol gets its own CSV file.

Output format (one row per WebSocket update):
  timestamp_ns, bid, ask, mid, spread

Usage:
  python data_collector.py [--symbols avaxusdt linkusdt aaveusdt injusdt]
  python data_collector.py --symbols btcusdt ethusdt    # quick 1-day capture

Files are written to research/data/<symbol>_<date>.csv
Existing files are appended to (safe to restart without losing data).
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("collector")

DATA_DIR = Path(__file__).parent.parent / "data"

DEFAULT_SYMBOLS = ["avaxusdt", "linkusdt", "aaveusdt", "injusdt"]

FLUSH_INTERVAL = 100   # flush CSV writer every N rows (reduces disk I/O)


# ---------------------------------------------------------------------------

def _ws_url(symbols: list[str]) -> str:
    streams = "/".join(f"{s}@bookTicker" for s in symbols)
    return f"wss://stream.binance.com:9443/stream?streams={streams}"


class SymbolWriter:
    """Buffered CSV writer for one symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._rows  = 0
        self._file  = None
        self._writer= None
        self._open_for_today()

    def _open_for_today(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        date_str  = datetime.now(timezone.utc).strftime("%Y%m%d")
        path      = DATA_DIR / f"{self.symbol}_{date_str}.csv"
        is_new    = not path.exists()
        self._file= open(path, "a", newline="", buffering=1)
        self._writer = csv.writer(self._file)
        if is_new:
            self._writer.writerow(["timestamp_ns", "bid", "ask", "mid", "spread"])
        log.info(f"[{self.symbol.upper():>10}] writing → {path}")

    def write(self, bid: float, ask: float) -> None:
        ts  = time.time_ns()
        mid = (bid + ask) / 2
        spd = ask - bid
        self._writer.writerow([ts, bid, ask, f"{mid:.8f}", f"{spd:.8f}"])
        self._rows += 1
        if self._rows % FLUSH_INTERVAL == 0:
            self._file.flush()
            # Roll file if date changed
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            if today not in self._file.name:
                self._file.close()
                self._open_for_today()

    def close(self) -> None:
        if self._file:
            self._file.flush()
            self._file.close()


# ---------------------------------------------------------------------------

async def collect(symbols: list[str]) -> None:
    writers = {s: SymbolWriter(s) for s in symbols}
    url     = _ws_url(symbols)
    counts  = {s: 0 for s in symbols}
    t_start = time.monotonic()

    log.info(f"Connecting — {len(symbols)} symbols")

    async for ws in websockets.connect(url, ping_interval=20):
        try:
            async for raw in ws:
                msg    = json.loads(raw)
                stream = msg.get("stream", "")
                data   = msg.get("data", {})
                sym    = stream.split("@")[0]
                if sym not in writers:
                    continue
                try:
                    bid = float(data["b"])
                    ask = float(data["a"])
                except (KeyError, ValueError):
                    continue

                writers[sym].write(bid, ask)
                counts[sym] += 1

                # Status log every 30s
                elapsed = time.monotonic() - t_start
                if elapsed > 0 and sum(counts.values()) % 5000 == 0:
                    rates = {s: c / elapsed for s, c in counts.items()}
                    log.info("  " + "  ".join(
                        f"{s.upper():>10}: {rates[s]:.1f} tick/s  ({c} rows)"
                        for s, c in counts.items()
                    ))

        except websockets.ConnectionClosed:
            log.warning("Disconnected — reconnecting in 2 s")
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Error: {e} — reconnecting in 5 s")
            await asyncio.sleep(5)

    for w in writers.values():
        w.close()


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Record Binance bid/ask to CSV")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                        metavar="SYM", help="Binance symbols (default: mid-tier set)")
    args = parser.parse_args()

    symbols = [s.lower() for s in args.symbols]
    log.info(f"Symbols: {', '.join(s.upper() for s in symbols)}")
    log.info(f"Output:  {DATA_DIR}/")
    log.info("Press Ctrl+C to stop")

    try:
        asyncio.run(collect(symbols))
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    main()
