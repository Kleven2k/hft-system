#!/usr/bin/env python3
"""
polygon_collector.py — Live US stock quote + trade collector via Polygon.io WebSocket.

Streams real-time quotes (bid/ask) and trades for a list of symbols.
Writes one CSV per symbol per day to research/data/alpaca/
(same format as alpaca_collector so backtests work on both).

CSV columns: timestamp,type,bid,ask,bid_size,ask_size,price,size

Usage:
  python research/collectors/polygon_collector.py

Environment variables (required):
  POLYGON_API_KEY — from polygon.io / massive.com dashboard

Or add to .env file in repo root:
  POLYGON_API_KEY=your_key_here
"""

import asyncio
import csv
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env if present
env_path = Path(__file__).parent.parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

try:
    import websockets
except ImportError:
    print("Missing dependency: pip install websockets")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SYMBOLS = ["AAPL", "MSFT", "AMD", "NVDA", "QQQ"]

DATA_DIR = Path(__file__).parent.parent.parent / "research" / "data" / "alpaca"
DATA_DIR.mkdir(parents=True, exist_ok=True)

WS_URL = "wss://socket.polygon.io/stocks"

LOG_INTERVAL = 300   # log stats every 5 minutes
RECONNECT_DELAY = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

class SymbolWriter:
    def __init__(self, symbol: str):
        self.symbol   = symbol
        self.date     = None
        self.file     = None
        self.writer   = None
        self.n_quotes = 0
        self.n_trades = 0
        self._open_file()

    def _open_file(self):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        if today == self.date:
            return
        if self.file:
            self.file.close()
        self.date = today
        path = DATA_DIR / f"{self.symbol.lower()}_{today}.csv"
        is_new = not path.exists()
        self.file   = open(path, "a", newline="")
        self.writer = csv.writer(self.file)
        if is_new:
            self.writer.writerow(["timestamp", "type", "bid", "ask",
                                  "bid_size", "ask_size", "price", "size"])

    def write_quote(self, ts: str, bid: float, ask: float,
                    bid_size: int, ask_size: int):
        self._open_file()
        self.writer.writerow([ts, "Q", bid, ask, bid_size, ask_size, "", ""])
        self.n_quotes += 1
        if (self.n_quotes + self.n_trades) % 5000 == 0:
            self.file.flush()

    def write_trade(self, ts: str, price: float, size: int):
        self._open_file()
        self.writer.writerow([ts, "T", "", "", "", "", price, size])
        self.n_trades += 1
        if (self.n_quotes + self.n_trades) % 5000 == 0:
            self.file.flush()

    def close(self):
        if self.file:
            self.file.flush()
            self.file.close()


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class PolygonCollector:
    def __init__(self):
        self.api_key = os.environ.get("POLYGON_API_KEY", "")
        if not self.api_key:
            log.error("Set POLYGON_API_KEY in .env or environment")
            sys.exit(1)
        self.writers  = {sym: SymbolWriter(sym) for sym in SYMBOLS}
        self.running  = True
        self.last_log = 0.0

    async def run(self):
        while self.running:
            try:
                await self._connect()
            except Exception as e:
                log.warning(f"Disconnected: {e} — reconnecting in {RECONNECT_DELAY}s")
                await asyncio.sleep(RECONNECT_DELAY)

    async def _connect(self):
        async with websockets.connect(WS_URL, ping_interval=20) as ws:
            # Wait for connected message
            msg = json.loads(await ws.recv())
            log.info(f"Connected: {msg}")

            # Authenticate
            await ws.send(json.dumps({"action": "auth", "params": self.api_key}))
            msg = json.loads(await ws.recv())
            log.info(f"Auth: {msg}")

            # Subscribe to quotes (Q) and trades (T)
            subs = ",".join(
                [f"Q.{s}" for s in SYMBOLS] + [f"T.{s}" for s in SYMBOLS]
            )
            await ws.send(json.dumps({"action": "subscribe", "params": subs}))
            msg = json.loads(await ws.recv())
            log.info(f"Subscribed: {msg}")

            log.info(f"Streaming {SYMBOLS} ...")

            async for raw in ws:
                if not self.running:
                    break
                messages = json.loads(raw)
                for m in messages:
                    self._handle(m)

    def _handle(self, msg: dict):
        ev  = msg.get("ev", "")
        sym = msg.get("sym", msg.get("S", ""))

        if ev == "Q" and sym in self.writers:
            # Polygon quote: bp=bid price, ap=ask price, bs=bid size, as=ask size
            ts = str(msg.get("t", msg.get("sip_timestamp", "")))
            self.writers[sym].write_quote(
                ts       = ts,
                bid      = msg.get("bp", 0.0),
                ask      = msg.get("ap", 0.0),
                bid_size = msg.get("bs", 0),
                ask_size = msg.get("as", 0),
            )

        elif ev == "T" and sym in self.writers:
            ts = str(msg.get("t", ""))
            self.writers[sym].write_trade(
                ts    = ts,
                price = msg.get("p", 0.0),
                size  = msg.get("s", 0),
            )

        # Periodic stats log
        import time
        now = time.monotonic()
        if now - self.last_log > LOG_INTERVAL:
            self.last_log = now
            parts = [f"{s}: {w.n_quotes}q/{w.n_trades}t"
                     for s, w in self.writers.items()]
            log.info("  " + "  |  ".join(parts))

    def stop(self):
        self.running = False
        for w in self.writers.values():
            w.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    collector = PolygonCollector()
    loop = asyncio.new_event_loop()

    def shutdown(*_):
        log.info("Shutting down ...")
        collector.stop()
        loop.stop()

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info(f"Polygon collector starting — symbols: {SYMBOLS}")
    log.info(f"Data dir: {DATA_DIR}")

    try:
        loop.run_until_complete(collector.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
