#!/usr/bin/env python3
"""
feed_bridge.py — Binance WebSocket book ticker → FPGA UDP market data bridge

Subscribes to Binance best-bid/ask for up to 4 symbols, converts to the FPGA's
20-byte binary format, and sends UDP packets to PORT_ITCH (17010) on the FPGA.

Also manages price_base via UART: when the market drifts more than DRIFT_LIMIT
ticks from price_base, it sends a new price_base frame so the order book's BRAM
index stays in range [0, 255].

FPGA UDP packet format (20 bytes, big-endian):
  [0]      msg_type:  0x41 = bid ADD  ('A')
                      0x42 = ask ADD  ('B')
                      0x43 = bid CANCEL ('C')
                      0x44 = ask CANCEL ('D')
  [1..8]   timestamp: nanoseconds since Unix epoch (uint64)
  [9..12]  price:     round(market_price / tick_size)  (uint32)
  [13..16] shares:    NOMINAL_SHARES  (uint32)
  [17..18] symbol_id: 0–3  (uint16)
  [19]     reserved:  0x00

UART price_base frame (5 bytes, 115200 8N1):
  [0]   0xA0 | slot[1:0]   (0xA0 = slot 0, 0xA1 = slot 1, ...)
  [1]   base[31:24]
  [2]   base[23:16]
  [3]   base[15:8]
  [4]   base[7:0]

UART kill-switch commands (1 byte):
  0xB0 = kill switch ON
  0xB1 = kill switch OFF

UART quote_offset frame (5 bytes, 115200 8N1):
  [0]   0xC0 | slot[1:0]   (0xC0 = slot 0, 0xC1 = slot 1, ...)
  [1]   offset[31:24]
  [2]   offset[23:16]
  [3]   offset[15:8]
  [4]   offset[7:0]

Instrument table (SYMBOLS):
  Each entry: (binance_symbol, fpga_symbol_id, tick_size_usd)
  tick_size_usd — 1 FPGA price tick in USD.  Choose so that:
    • typical spread ≈ 1–20 ticks  (good resolution)
    • 256 ticks covers expected intraday move between price_base updates

Usage:
  pip install websockets pyserial
  python feed_bridge.py [--paper]   # --paper: kill_switch OFF, OUCH goes to ack_simulator
  python feed_bridge.py --live       # --live:  kill_switch OFF, OUCH goes to real exchange (USE WITH CAUTION)
"""

import argparse
import asyncio
import json
import socket
import struct
import time
import threading
import logging

import serial
import websockets

# ---------------------------------------------------------------------------
# Configuration — edit these to match your setup
# ---------------------------------------------------------------------------

FPGA_IP   = "192.168.1.10"   # LOCAL_IP from eth_stack_wrapper.sv
FPGA_PORT = 17010             # PORT_ITCH from hft_pkg.sv
UART_PORT = "COM5"            # set_price_base.py already uses this
UART_BAUD = 115200

# Symbols: (binance_ws_symbol, fpga_symbol_id, tick_size_usd)
#
# tick_size_usd examples:
#   ETH @ $3000, spread ~$0.05  → 0.01  gives 5-tick spread, 256 ticks = $2.56
#   SOL @ $150,  spread ~$0.01  → 0.001 gives 10-tick spread, 256 ticks = $0.256
#   BNB @ $500,  spread ~$0.05  → 0.01  gives 5-tick spread
#   BTC @ $70k,  spread ~$1     → 1.0   gives 1-tick spread,  256 ticks = $256
#
# Start with just one symbol to keep things simple.  Add more once working.
# Symbols: (binance_ws_symbol, fpga_symbol_id, tick_size_usd, drift_limit_ticks)
#
# tick_size_usd  — 1 FPGA tick in USD.  Must resolve the exchange's minimum
#                  price increment as ≥1 tick (tick_size ≤ min_increment).
# drift_limit    — send UART price_base update when bid drifts this many ticks.
#                  Should be ~200 ticks of intraday move in that instrument.
#
#   ETH min increment $0.01 → tick=0.01, $2 drift → drift_limit=200
#   SOL min increment $0.001→ tick=0.001,$0.20 drift→ drift_limit=200
#   BNB min increment $0.01 → tick=0.01, $2 drift → drift_limit=200
#   BTC min increment $0.01 → tick=0.01, $20 drift → drift_limit=2000
#     (was tick=0.1 → both bid and ask rounded to same tick, spread=0)
SYMBOLS = [
    # Mid-tier tokens: wider spreads (5-30 ticks), less HFT competition than BTC/ETH.
    # tick_size chosen so 1 tick = Binance min price increment for that pair.
    ("avaxusdt", 0, 0.01,  200),   # AVAX  ~$35,  spread ~5-20 ticks,  256 ticks=$2.56
    ("linkusdt", 1, 0.001, 200),   # LINK  ~$15,  spread ~5-15 ticks,  256 ticks=$0.256
    ("aaveusdt", 2, 0.01,  200),   # AAVE  ~$200, spread ~5-20 ticks,  256 ticks=$2.56
    ("injusdt",  3, 0.001, 200),   # INJ   ~$25,  spread ~5-20 ticks,  256 ticks=$0.256
]

# Per-slot quote offset in price ticks (UART-sent to FPGA at startup).
# Based on backtest parameter sweep over 7 days of 1s klines.
# Change and restart feed_bridge.py to retune — no FPGA rebuild needed.
QUOTE_OFFSETS = {
    0: 3,    # AVAX:  3 ticks = $0.03  (Sharpe 5.12 at offset=3, stale=2)
    1: 10,   # LINK: 10 ticks = $0.01  (consistent $31/3d at 2.5% fill rate)
    2: 5,    # AAVE:  5 ticks = $0.05  (high maker rebate per fill ~$1)
    3: 3,    # INJ:   3 ticks = $0.003 (Sharpe 2.20 at offset=3, stale=2)
}

# Order book has MAX_LEVELS=256.  price_idx = price[7:0] - price_base[7:0].
BASE_OFFSET = 50   # ticks — price_base sits this many ticks below current bid

# ---------------------------------------------------------------------------
# Stop-loss: monitor telemetry P&L and halt if any slot bleeds past threshold.
# The FPGA sends a 64-byte telemetry UDP packet to PORT_TELEM once per second.
# pnl in the packet is signed int32; monitor.py converts: dollars = pnl/10000.
# When any slot's P&L drops below -SL_THRESHOLD_USD, kill_switch is set ON.
# Set SL_THRESHOLD_USD = None to disable.
# ---------------------------------------------------------------------------
TELEM_PORT        = 42002
SL_THRESHOLD_USD  = 20.0   # halt when any slot loses more than $20

# Shares reported in each quote message (just needs to be non-zero;
# the FPGA order book only needs to know a level is populated).
NOMINAL_SHARES = 100_000

# Binance combined book ticker WebSocket URL
WS_URL = "wss://stream.binance.com:9443/stream?streams=" + "/".join(
    f"{sym}@bookTicker" for sym, *_ in SYMBOLS
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("feed_bridge")

# ---------------------------------------------------------------------------
# UART helpers
# ---------------------------------------------------------------------------

_uart_lock = threading.Lock()
_uart_ser: "serial.Serial | None" = None


def _uart_open() -> None:
    """Open the UART port once and keep it open for the session."""
    global _uart_ser
    if _uart_ser is None or not _uart_ser.is_open:
        _uart_ser = serial.Serial(UART_PORT, UART_BAUD, timeout=1)
        log.info(f"UART  opened {UART_PORT} @ {UART_BAUD}")


def _uart_write(data: bytes) -> None:
    """Write bytes to the open UART port, reopening on error."""
    global _uart_ser
    with _uart_lock:
        try:
            _uart_open()
            _uart_ser.write(data)
        except Exception as e:
            log.error(f"UART  write error: {e} — will retry on next update")
            try:
                _uart_ser.close()
            except Exception:
                pass
            _uart_ser = None


def uart_set_price_base(slot: int, base: int) -> None:
    """Send a 5-byte price_base frame to the FPGA via UART."""
    frame = bytes([0xA0 | (slot & 0x3)]) + struct.pack(">I", base & 0xFFFF_FFFF)
    _uart_write(frame)
    log.info(f"UART  price_base[{slot}] = {base}")


def uart_kill_switch(on: bool) -> None:
    """Send kill-switch ON (0xB0) or OFF (0xB1) command."""
    _uart_write(bytes([0xB0 if on else 0xB1]))
    log.info(f"UART  kill_switch = {'ON' if on else 'OFF'}")


def uart_set_quote_offset(slot: int, offset_ticks: int) -> None:
    """Send a 5-byte quote_offset frame (0xC0|slot + 4-byte big-endian value)."""
    frame = bytes([0xC0 | (slot & 0x3)]) + struct.pack(">I", offset_ticks & 0xFFFF_FFFF)
    _uart_write(frame)
    log.info(f"UART  quote_offset[{slot}] = {offset_ticks} ticks")

# ---------------------------------------------------------------------------
# UDP helpers
# ---------------------------------------------------------------------------

_udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _build_packet(msg_type: int, price: int, shares: int, symbol_id: int) -> bytes:
    """Pack a 20-byte FPGA market-data message."""
    ts_ns = time.time_ns()
    return struct.pack(">BQIIHx",
                       msg_type,   # [0]
                       ts_ns,      # [1..8]  uint64 big-endian
                       price,      # [9..12] uint32
                       shares,     # [13..16] uint32
                       symbol_id)  # [17..18] uint16, [19] reserved (x=pad)


def send_quote(msg_type: int, price: int, symbol_id: int) -> None:
    pkt = _build_packet(msg_type, price, NOMINAL_SHARES, symbol_id)
    _udp_sock.sendto(pkt, (FPGA_IP, FPGA_PORT))

# ---------------------------------------------------------------------------
# Per-symbol state
# ---------------------------------------------------------------------------

class SymbolState:
    def __init__(self, name: str, slot: int, tick: float, drift_limit: int):
        self.name        = name
        self.slot        = slot
        self.tick        = tick          # USD per FPGA tick
        self.drift_limit  = drift_limit   # ticks before price_base update
        self.price_base   = 0             # current FPGA price_base for this slot
        self.prev_bid     = 0             # last bid price sent (FPGA ticks)
        self.prev_ask     = 0             # last ask price sent (FPGA ticks)
        self.initialized  = False
        self.last_base_t  = 0.0           # monotonic time of last price_base update

    def to_ticks(self, usd_price: float) -> int:
        return round(usd_price / self.tick)

    # Minimum seconds between price_base UART updates.
    # Prevents spam during fast moves (e.g. BTC dropping $70 in 90 seconds).
    BASE_UPDATE_MIN_INTERVAL = 2.0

    def needs_base_update(self, bid_ticks: int) -> bool:
        """True if bid has drifted outside [price_base, price_base+drift_limit]
        AND enough time has passed since the last update."""
        if self.price_base == 0:
            return True
        drift = bid_ticks - self.price_base
        out_of_range = drift < 0 or drift > self.drift_limit
        throttled    = (time.monotonic() - self.last_base_t) < self.BASE_UPDATE_MIN_INTERVAL
        return out_of_range and not throttled

    def reset_base(self, bid_ticks: int) -> None:
        """Set price_base = bid_ticks - BASE_OFFSET and send UART frame."""
        new_base = max(0, bid_ticks - BASE_OFFSET)
        self.price_base  = new_base
        self.last_base_t = time.monotonic()
        uart_set_price_base(self.slot, new_base)
        # Flush stale book levels: cancel old bid/ask so the BRAM doesn't
        # hold ghost quantities at the old price_idx slots.
        if self.prev_bid:
            send_quote(0x43, self.prev_bid, self.slot)  # bid CANCEL
        if self.prev_ask:
            send_quote(0x44, self.prev_ask, self.slot)  # ask CANCEL
        self.prev_bid = 0
        self.prev_ask = 0

    def on_update(self, bid_usd: float, ask_usd: float) -> None:
        bid = self.to_ticks(bid_usd)
        ask = self.to_ticks(ask_usd)

        # Update price_base if needed (first time or after drift)
        if self.needs_base_update(bid):
            self.reset_base(bid)

        # Cancel old level, add new level — only when price changed
        if bid != self.prev_bid:
            if self.prev_bid:
                send_quote(0x43, self.prev_bid, self.slot)  # bid CANCEL
            send_quote(0x41, bid, self.slot)                 # bid ADD
            self.prev_bid = bid

        if ask != self.prev_ask:
            if self.prev_ask:
                send_quote(0x44, self.prev_ask, self.slot)  # ask CANCEL
            send_quote(0x42, ask, self.slot)                 # ask ADD
            self.prev_ask = ask

        if not self.initialized:
            spread_ticks = ask - bid
            log.info(
                f"[{self.name.upper():>8}] init  bid={bid_usd:.4f}  ask={ask_usd:.4f}"
                f"  spread={spread_ticks} ticks  base={self.price_base}"
            )
            self.initialized = True

# ---------------------------------------------------------------------------
# Stop-loss monitor (background thread, reads FPGA telemetry on port 42002)
# ---------------------------------------------------------------------------

_TELEM_MAGIC  = b'\x48\x46\x54\x01'
_sl_triggered = False   # set once; prevents repeated UART writes


def _stop_loss_monitor() -> None:
    """Background thread: parse telemetry and kill the strategy on large loss."""
    global _sl_triggered
    if SL_THRESHOLD_USD is None:
        return

    threshold_ticks = int(SL_THRESHOLD_USD * 10_000)
    # Listen on localhost relay port forwarded by monitor.py (avoids port conflict on 42002)
    SL_RELAY_PORT = 42003
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", SL_RELAY_PORT))
    sock.settimeout(2.0)
    log.info(f"SL    monitor active — threshold −${SL_THRESHOLD_USD:.2f} per slot  (relay port {SL_RELAY_PORT})")

    import struct as _struct
    while True:
        try:
            data, _ = sock.recvfrom(256)
        except socket.timeout:
            continue
        if len(data) < 64 or data[:4] != _TELEM_MAGIC:
            continue

        for i in range(4):
            off = 16 + i * 12
            pnl, = _struct.unpack_from(">i", data, off + 4)
            if pnl < -threshold_ticks and not _sl_triggered:
                _sl_triggered = True
                dollars = pnl / 10_000.0
                log.warning(
                    f"SL    TRIGGERED  slot={i}  P&L=${dollars:.4f}"
                    f"  < −${SL_THRESHOLD_USD:.2f}  → kill_switch ON"
                )
                uart_kill_switch(on=True)
                # Don't break — log all breaching slots this packet
                # but only send kill once (_sl_triggered gate)


def start_stop_loss_monitor() -> None:
    if SL_THRESHOLD_USD is None:
        return
    t = threading.Thread(target=_stop_loss_monitor, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Main WebSocket loop
# ---------------------------------------------------------------------------

async def run(paper_mode: bool) -> None:
    # Build symbol lookup: binance stream name → SymbolState
    states = {
        sym: SymbolState(sym, slot, tick, drift)
        for sym, slot, tick, drift in SYMBOLS
    }

    log.info(f"Connecting to {WS_URL}")
    log.info(f"FPGA target: {FPGA_IP}:{FPGA_PORT}")
    # Start with kill_switch ON — released only after all price_bases are configured.
    # Default hft_top.sv price_base values are wrong for current symbols; if orders
    # fire before UART corrects price_base, the order book returns garbled prices and
    # we get catastrophic fills (e.g. BUY AAVE at $5000 when market is $112).
    log.info(f"Kill switch: ON (held during init — released after all symbols configured)")
    uart_kill_switch(on=True)
    for slot, ticks in QUOTE_OFFSETS.items():
        uart_set_quote_offset(slot, ticks)
    start_stop_loss_monitor()

    _kill_released = False

    async for ws in websockets.connect(WS_URL, ping_interval=20):
        try:
            log.info("WebSocket connected")
            async for raw in ws:
                msg = json.loads(raw)
                stream = msg.get("stream", "")        # e.g. "ethusdt@bookTicker"
                data   = msg.get("data", {})
                sym    = stream.split("@")[0]          # e.g. "ethusdt"

                if sym not in states:
                    continue

                try:
                    bid_usd = float(data["b"])
                    ask_usd = float(data["a"])
                except (KeyError, ValueError):
                    continue

                states[sym].on_update(bid_usd, ask_usd)

                # Release kill_switch once every symbol has sent its price_base.
                if not _kill_released and all(s.initialized for s in states.values()):
                    _kill_released = True
                    log.info("All symbols initialized — releasing kill switch")
                    uart_kill_switch(on=False)

        except websockets.ConnectionClosed:
            log.warning("WebSocket disconnected — reconnecting in 2 s")
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"WebSocket error: {e} — reconnecting in 5 s")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Binance → FPGA market data bridge")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--paper", action="store_true", default=True,
                      help="Paper mode: OUCH to ack_simulator (default)")
    mode.add_argument("--live",  action="store_true", default=False,
                      help="Live mode: OUCH to real exchange (be careful)")
    args = parser.parse_args()

    paper = not args.live
    try:
        asyncio.run(run(paper_mode=paper))
    except KeyboardInterrupt:
        log.info("Shutting down — setting kill_switch ON")
        uart_kill_switch(on=True)


if __name__ == "__main__":
    main()
