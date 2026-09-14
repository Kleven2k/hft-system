#!/usr/bin/env python3
"""
itch_replay.py — Replay historical NASDAQ tick data to the FPGA (Phase 31)

Reads a BookTick CSV (produced by research/nasdaq/itch_rs) and streams it to
the FPGA as UDP market-data packets, in the same 20-byte wire format
feed_bridge.py uses for live Binance data. This is the hardware-facing
counterpart to fpga/tb/nasdaq/ — that testbench proves strategy.sv's logic in
simulation; this drives the real bitstream end to end (parser → order book →
strategy → OUCH), so you can paper-trade real NASDAQ history on the board.

NASDAQ has no retail direct-market-access path, so fills come from
software/exchange_sim.py (or ack_simulator.py) exactly as they do for
crypto paper mode. Nothing here talks to a real exchange.

FPGA UDP packet format (20 bytes, big-endian) — must match
market_data_parser.sv and feed_bridge.py:
  [0]      msg_type: 0x41 bid ADD / 0x42 ask ADD / 0x43 bid CANCEL / 0x44 ask CANCEL
  [1..8]   timestamp (uint64)
  [9..12]  price in FPGA ticks (uint32)
  [13..16] shares (uint32)
  [17..18] symbol_id (uint16)
  [19]     reserved

Usage:
  # Replay at 100x real speed into slot 0
  python software/itch_replay.py --file research/data/nasdaq/aapl_20200130_ticks.csv --speed 100

  # Replay as fast as the FPGA will take it (throughput/soak test)
  python software/itch_replay.py --file ... --speed 0

  # Real-time replay (takes a full trading day)
  python software/itch_replay.py --file ... --speed 1
"""

import argparse
import csv
import logging
import socket
import struct
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:
    serial = None

# ---------------------------------------------------------------------------
# Configuration — must match feed_bridge.py / hft_top.sv
# ---------------------------------------------------------------------------

FPGA_IP   = "192.168.1.10"
FPGA_PORT = 17010          # PORT_ITCH from hft_pkg.sv
UART_PORT = "COM5"
UART_BAUD = 115200

TICK_SIZE_USD = 0.01       # NASDAQ minimum price increment for these symbols

# Order book has MAX_LEVELS=256; price_idx = price - price_base must land in
# [0, 255] for BOTH bid and ask or the BRAM write goes out of range and the
# book starts returning garbage prices (which is how you get catastrophic
# fills — see the kill-switch note in feed_bridge.py).
MAX_LEVELS   = 256         # must match order_book.sv MAX_LEVELS
BASE_OFFSET  = 50          # ticks — price_base sits this far below current bid
DRIFT_LIMIT  = 200         # ticks of drift before a new price_base is sent

# A quote whose spread can't fit the window at all is unrepresentable, no
# matter where price_base sits. Real NASDAQ data contains a handful of these:
# during book warm-up the reconstructed book can show a stale resting bid
# (e.g. $1.01 against a $174 ask — a 17,389-tick spread), and they also appear
# in extreme dislocations. Dropping them is correct: they aren't quotable
# markets, and writing them would corrupt the book for every later row.
MAX_SPREAD_TICKS = MAX_LEVELS - BASE_OFFSET - 1   # 205

NOMINAL_SHARES = 100_000   # book only needs "level populated", not real size

MSG_BID_ADD    = 0x41
MSG_ASK_ADD    = 0x42
MSG_BID_CANCEL = 0x43
MSG_ASK_CANCEL = 0x44

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("itch_replay")

_udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_uart_ser = None


# ---------------------------------------------------------------------------
# UART (price_base / kill switch) — same frame format as feed_bridge.py
# ---------------------------------------------------------------------------

def uart_open(port: str, baud: int):
    global _uart_ser
    if serial is None:
        log.warning("pyserial not installed — UART disabled (price_base will NOT be set)")
        return
    try:
        _uart_ser = serial.Serial(port, baud, timeout=1)
        log.info(f"UART  opened {port} @ {baud}")
    except Exception as e:
        log.error(f"UART  open failed: {e} — price_base will NOT be set")
        _uart_ser = None


def uart_write(data: bytes) -> None:
    if _uart_ser is None:
        return
    try:
        _uart_ser.write(data)
    except Exception as e:
        log.error(f"UART  write error: {e}")


def uart_set_price_base(slot: int, base: int) -> None:
    uart_write(bytes([0xA0 | (slot & 0x3)]) + struct.pack(">I", base & 0xFFFF_FFFF))
    log.info(f"UART  price_base[{slot}] = {base}")


def uart_set_quote_offset(slot: int, offset_ticks: int) -> None:
    uart_write(bytes([0xC0 | (slot & 0x3)]) + struct.pack(">I", offset_ticks & 0xFFFF_FFFF))
    log.info(f"UART  quote_offset[{slot}] = {offset_ticks} ticks")


def uart_kill_switch(on: bool) -> None:
    uart_write(bytes([0xB0 if on else 0xB1]))
    log.info(f"UART  kill_switch = {'ON' if on else 'OFF'}")


# ---------------------------------------------------------------------------
# UDP market data
# ---------------------------------------------------------------------------

def build_packet(msg_type: int, price: int, shares: int, symbol_id: int,
                 ts_ns: int) -> bytes:
    return struct.pack(">BQIIHx", msg_type, ts_ns, price, shares, symbol_id)


def send_quote(msg_type: int, price: int, symbol_id: int, ts_ns: int) -> None:
    _udp_sock.sendto(build_packet(msg_type, price, NOMINAL_SHARES, symbol_id, ts_ns),
                     (FPGA_IP, FPGA_PORT))


class SlotState:
    """Tracks price_base drift and last-sent levels for one FPGA slot.

    Mirrors feed_bridge.py's SymbolState, minus the WebSocket/throttle parts:
    replay has no wall-clock pressure, so price_base updates are sent
    whenever the data drifts rather than being rate-limited.
    """

    def __init__(self, slot: int):
        self.slot       = slot
        self.price_base = 0
        self.prev_bid   = 0
        self.prev_ask   = 0
        self.n_base_updates = 0
        self.n_skipped  = 0
        self.initialized = False

    def needs_base_update(self, bid_ticks: int, ask_ticks: int) -> bool:
        """True when either side would fall outside the BRAM window.

        feed_bridge.py only checks the bid; that's survivable for crypto,
        where spreads are small next to DRIFT_LIMIT, but on NASDAQ data the
        ask can escape the window while the bid is still comfortably inside.
        """
        if self.price_base == 0:
            return True
        if bid_ticks - self.price_base < 0:
            return True
        return (ask_ticks - self.price_base) > DRIFT_LIMIT

    def reset_base(self, bid_ticks: int, ts_ns: int) -> None:
        self.price_base = max(0, bid_ticks - BASE_OFFSET)
        self.n_base_updates += 1
        uart_set_price_base(self.slot, self.price_base)
        # Flush stale levels so the BRAM doesn't keep ghost quantities at the
        # old price_idx slots.
        if self.prev_bid:
            send_quote(MSG_BID_CANCEL, self.prev_bid, self.slot, ts_ns)
        if self.prev_ask:
            send_quote(MSG_ASK_CANCEL, self.prev_ask, self.slot, ts_ns)
        self.prev_bid = 0
        self.prev_ask = 0

    def on_update(self, bid_ticks: int, ask_ticks: int, ts_ns: int) -> bool:
        """Push one book update. Returns False if the row was skipped."""
        if ask_ticks - bid_ticks > MAX_SPREAD_TICKS:
            self.n_skipped += 1
            return False

        if self.needs_base_update(bid_ticks, ask_ticks):
            self.reset_base(bid_ticks, ts_ns)

        if bid_ticks != self.prev_bid:
            if self.prev_bid:
                send_quote(MSG_BID_CANCEL, self.prev_bid, self.slot, ts_ns)
            send_quote(MSG_BID_ADD, bid_ticks, self.slot, ts_ns)
            self.prev_bid = bid_ticks

        if ask_ticks != self.prev_ask:
            if self.prev_ask:
                send_quote(MSG_ASK_CANCEL, self.prev_ask, self.slot, ts_ns)
            send_quote(MSG_ASK_ADD, ask_ticks, self.slot, ts_ns)
            self.prev_ask = ask_ticks

        self.initialized = True
        return True


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def usd_to_ticks(usd: float) -> int:
    return round(usd / TICK_SIZE_USD)


def iter_rows(path: Path, max_rows: int):
    """Yield (ts_ns, bid_ticks, ask_ticks) for rows with a complete book."""
    with open(path, newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if max_rows and i >= max_rows:
                break
            if not row["best_bid"] or not row["best_ask"]:
                continue
            yield (int(row["ts_ns"]),
                   usd_to_ticks(float(row["best_bid"])),
                   usd_to_ticks(float(row["best_ask"])))


def replay(path: Path, slot: int, speed: float, max_rows: int) -> None:
    state = SlotState(slot)
    n_rows = 0
    first_ts = None
    wall_start = time.monotonic()

    for ts_ns, bid, ask in iter_rows(path, max_rows):
        if first_ts is None:
            # Prime price_base before releasing the kill switch — a wrong
            # price_base makes the order book emit garbage prices, and orders
            # fired against those get catastrophic fills (same reasoning as
            # feed_bridge.py). The first rows of a reconstructed book can be
            # unrepresentable, so keep trying until one lands.
            if not state.on_update(bid, ask, ts_ns):
                continue
            first_ts = ts_ns
            wall_start = time.monotonic()
            uart_kill_switch(on=False)
            log.info(f"Replaying {path.name} → slot {slot} at speed={speed or 'max'}")
            n_rows += 1
            continue

        if speed > 0:
            target = (ts_ns - first_ts) / 1e9 / speed
            lag = target - (time.monotonic() - wall_start)
            if lag > 0:
                time.sleep(lag)

        state.on_update(bid, ask, ts_ns)
        n_rows += 1

        if n_rows % 100_000 == 0:
            elapsed = time.monotonic() - wall_start
            log.info(f"  {n_rows:,} rows  ({n_rows/max(elapsed,1e-9):,.0f} rows/s)"
                     f"  price_base updates: {state.n_base_updates}"
                     f"  skipped: {state.n_skipped}")

    elapsed = time.monotonic() - wall_start
    log.info(f"Done. {n_rows:,} rows in {elapsed:.1f}s "
             f"({n_rows/max(elapsed,1e-9):,.0f} rows/s), "
             f"{state.n_base_updates} price_base updates, "
             f"{state.n_skipped} rows skipped (spread > {MAX_SPREAD_TICKS} ticks)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay NASDAQ tick data to the FPGA")
    ap.add_argument("--file", required=True, help="BookTick CSV from itch_rs")
    ap.add_argument("--slot", type=int, default=0, help="FPGA symbol slot (0-3)")
    ap.add_argument("--speed", type=float, default=100.0,
                    help="Replay speed multiple vs real time; 0 = as fast as possible")
    ap.add_argument("--max-rows", type=int, default=0, help="Stop after N rows (0 = all)")
    ap.add_argument("--quote-offset", type=int, default=0,
                    help="quote_offset in ticks (0 = quote at the touch, the "
                         "validated NASDAQ parameter)")
    ap.add_argument("--uart-port", default=UART_PORT)
    ap.add_argument("--no-uart", action="store_true",
                    help="Skip UART entirely (price_base will NOT be set — "
                         "only safe if the FPGA is already configured)")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        log.error(f"File not found: {path}")
        sys.exit(1)

    if not args.no_uart:
        uart_open(args.uart_port, UART_BAUD)
        # Hold orders off until price_base is primed from the first row.
        uart_kill_switch(on=True)
        uart_set_quote_offset(args.slot, args.quote_offset)

    try:
        replay(path, args.slot, args.speed, args.max_rows)
    except KeyboardInterrupt:
        log.info("Interrupted — halting strategy")
    finally:
        if not args.no_uart:
            uart_kill_switch(on=True)


if __name__ == "__main__":
    main()
