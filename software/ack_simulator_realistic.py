#!/usr/bin/env python3
"""
ack_simulator_realistic.py — Realistic fill simulation using live Binance order book.

Unlike ack_simulator.py (which always fills), this version only fills a limit
order if the Binance best price crosses the quoted level before the order is
cancelled or the fill window expires.

Fill logic:
  BUY  at FPGA-ticks P  fills when  best_ask_usd  ≤  P * tick_size
  SELL at FPGA-ticks P  fills when  best_bid_usd  ≥  P * tick_size

When the FPGA cancels an order (stale detection) it sends a 0x58 cancel frame.
We honour that cancel and send back ACK_CANCELLED so the FPGA OMS clears state.

If price never crosses within FILL_WINDOW_MS, we also send ACK_CANCELLED.

P&L is tracked in Python (not sent to FPGA) per symbol:
  BUY  fill:  edge = mid_at_fill - fill_price_usd
  SELL fill:  edge = fill_price_usd - mid_at_fill
  Plus maker rebate: fill_notional * MAKER_REBATE_BPS / 10000

Usage:
  pip install websockets
  python ack_simulator_realistic.py [--window 5000]
"""

import argparse
import asyncio
import json
import logging
import socket
import struct
import threading
import time

import websockets

# ---------------------------------------------------------------------------
# Configuration — must match hft_pkg.sv + feed_bridge.py
# ---------------------------------------------------------------------------

LISTEN_IP   = "0.0.0.0"
LISTEN_PORT = 42000        # PORT_OUCH — FPGA sends orders here
FPGA_IP     = "192.168.1.10"
ACK_PORT    = 42001        # PORT_OUCH_ACK — FPGA listens for ACKs

# How long to wait for a fill before cancelling (order lifetime in ms).
# The FPGA's stale detection usually cancels first (via 0x58), so this is a
# safety net for orders that linger without a stale cancel.
# Must be >= FPGA TIMEOUT_CYC (currently 10 s) so the FPGA's timeout always
# fires before this window, keeping the ack_sim from cancelling prematurely.
FILL_WINDOW_MS = 15_000

# Maker rebate in basis points (1 bp = 0.01%).
# Binance standard maker rebate: 1 bp = +0.01% (you receive money).
MAKER_REBATE_BPS = 1.0

# Symbol table — must match SYMBOLS in feed_bridge.py
# (binance_stream_name, fpga_symbol_id, tick_size_usd)
SYMBOLS_CFG = [
    ("avaxusdt", 0, 0.01),
    ("linkusdt", 1, 0.001),
    ("aaveusdt", 2, 0.01),
    ("injusdt",  3, 0.001),
]

# Derived lookups
_ID_TO_CFG   = {sid: (name, tick) for name, sid, tick in SYMBOLS_CFG}
_STREAM_TO_ID = {name: sid         for name, sid, _    in SYMBOLS_CFG}

WS_URL = "wss://stream.binance.com:9443/stream?streams=" + "/".join(
    f"{name}@bookTicker" for name, _, _ in SYMBOLS_CFG
)

LOG_EVERY_N = 10   # log every Nth order to stdout

# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("ack_sim_r")

ACK_FILLED    = 0x00
ACK_CANCELLED = 0x03

# ---------------------------------------------------------------------------
# Shared state (all writes protected by _lock)
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# Live best bid/ask per symbol_id
_book: dict[int, dict] = {sid: {"bid": 0.0, "ask": 0.0} for sid, _ in _ID_TO_CFG.items()}

# Pending orders: order_id → dict
#   price_usd, side ("BUY"|"SELL"), symbol_id, qty, timer
_pending: dict[int, dict] = {}

# Orders the FPGA has cancelled — must not fill these
_cancelled: set[int] = set()

# Per-symbol P&L (Python-side)
_pnl:       dict[int, float] = {sid: 0.0 for sid in _ID_TO_CFG}
_fills:     dict[int, int]   = {sid: 0   for sid in _ID_TO_CFG}
_no_fills:  dict[int, int]   = {sid: 0   for sid in _ID_TO_CFG}
_orders:    int = 0
_cancels:   int = 0

# ---------------------------------------------------------------------------
# ACK sender
# ---------------------------------------------------------------------------

_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _send_ack(order_id: int, status: int, qty: int) -> None:
    _tx.sendto(struct.pack(">QBI", order_id, status, qty), (FPGA_IP, ACK_PORT))


# ---------------------------------------------------------------------------
# Fill helpers  (must be called with _lock held)
# ---------------------------------------------------------------------------

def _price_crosses(order: dict) -> bool:
    """Return True if current book price crosses the order's limit."""
    bs = _book[order["symbol_id"]]
    if bs["bid"] == 0.0:
        return False
    if order["side"] == "BUY"  and bs["ask"] <= order["price_usd"]:
        return True
    if order["side"] == "SELL" and bs["bid"] >= order["price_usd"]:
        return True
    return False


def _do_fill(order_id: int) -> None:
    """Execute fill, update P&L, send ACK.  Called with _lock held."""
    o   = _pending.pop(order_id)
    sid = o["symbol_id"]
    bs  = _book[sid]
    mid = (bs["bid"] + bs["ask"]) / 2.0
    _, tick = _ID_TO_CFG[sid]

    edge    = (mid - o["price_usd"]) if o["side"] == "BUY" else (o["price_usd"] - mid)
    rebate  = o["price_usd"] * o["qty"] * MAKER_REBATE_BPS / 10_000
    trade_p = edge + rebate
    _pnl[sid]   += trade_p
    _fills[sid] += 1

    _send_ack(order_id, ACK_FILLED, o["qty"])
    dp = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 2
    log.info(
        f"FILL  id={order_id:#018x}  sym={sid}  {o['side']}"
        f"  price=${o['price_usd']:.{dp}f}  mid=${mid:.{dp}f}"
        f"  edge=${edge:+.4f}  rebate=${rebate:.5f}"
        f"  cum_pnl[{sid}]=${_pnl[sid]:+.4f}"
    )


# ---------------------------------------------------------------------------
# Book update → immediate fill check
# ---------------------------------------------------------------------------

def _on_book_update(symbol_id: int, bid: float, ask: float) -> None:
    """Called from WebSocket thread on each bookTicker message."""
    with _lock:
        _book[symbol_id] = {"bid": bid, "ask": ask}
        to_fill = [
            oid for oid, o in _pending.items()
            if o["symbol_id"] == symbol_id
            and oid not in _cancelled
            and _price_crosses(o)
        ]
        for oid in to_fill:
            _do_fill(oid)


# ---------------------------------------------------------------------------
# Fill-window timeout (no fill within FILL_WINDOW_MS → cancel)
# ---------------------------------------------------------------------------

def _fill_timeout(order_id: int) -> None:
    with _lock:
        if order_id not in _pending:
            return   # already filled or cancelled by FPGA
        sid = _pending[order_id]["symbol_id"]
        del _pending[order_id]
        _no_fills[sid] += 1
    _send_ack(order_id, ACK_CANCELLED, 0)
    log.debug(f"TIMEOUT  id={order_id:#018x}  no fill within {FILL_WINDOW_MS} ms")


# ---------------------------------------------------------------------------
# Binance WebSocket listener (runs in its own thread via asyncio)
# ---------------------------------------------------------------------------

async def _ws_loop() -> None:
    log.info(f"WS    connecting — {len(SYMBOLS_CFG)} symbols")
    async for ws in websockets.connect(WS_URL, ping_interval=20):
        try:
            async for raw in ws:
                msg    = json.loads(raw)
                stream = msg.get("stream", "")
                data   = msg.get("data", {})
                name   = stream.split("@")[0]
                if name not in _STREAM_TO_ID:
                    continue
                try:
                    bid = float(data["b"])
                    ask = float(data["a"])
                except (KeyError, ValueError):
                    continue
                _on_book_update(_STREAM_TO_ID[name], bid, ask)
        except websockets.ConnectionClosed:
            log.warning("WS disconnected — reconnecting in 2 s")
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"WS error: {e} — reconnecting in 5 s")
            await asyncio.sleep(5)


def _run_ws() -> None:
    asyncio.run(_ws_loop())


# ---------------------------------------------------------------------------
# P&L periodic summary
# ---------------------------------------------------------------------------

def _pnl_summary() -> None:
    while True:
        time.sleep(30)
        with _lock:
            total = sum(_pnl.values())
            parts = []
            for sid, (name, _) in _ID_TO_CFG.items():
                fr = _fills[sid] / max(1, _fills[sid] + _no_fills[sid])
                parts.append(
                    f"  {name.upper():>8}  pnl=${_pnl[sid]:+8.4f}"
                    f"  fills={_fills[sid]}  no-fill={_no_fills[sid]}"
                    f"  fill-rate={fr:.0%}"
                )
        log.info("─── P&L ─────────────────────────────────────────")
        for p in parts:
            log.info(p)
        log.info(f"  {'TOTAL':>8}  pnl=${total:+8.4f}")


# ---------------------------------------------------------------------------
# OUCH listener (main thread)
# ---------------------------------------------------------------------------

def _ouch_loop(window_ms: int) -> None:
    global _orders, _cancels
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind((LISTEN_IP, LISTEN_PORT))
    rx.settimeout(1.0)
    log.info(f"OUCH  listening on :{LISTEN_PORT}"
             f"  fill_window={window_ms} ms"
             f"  rebate={MAKER_REBATE_BPS} bps")

    while True:
        try:
            data, _ = rx.recvfrom(64)
        except socket.timeout:
            continue

        msg_type = data[0]

        # ── New order (0x4F 'O') ─────────────────────────────────────────
        if msg_type == 0x4F and len(data) >= 20:
            sym_id   = struct.unpack_from(">H", data, 1)[0]
            side_b   = data[3]
            price_t  = struct.unpack_from(">I", data, 4)[0]
            qty      = struct.unpack_from(">I", data, 8)[0]
            order_id = struct.unpack_from(">Q", data, 12)[0]

            if sym_id not in _ID_TO_CFG:
                log.warning(f"Unknown symbol_id={sym_id}")
                continue

            _, tick = _ID_TO_CFG[sym_id]
            price_usd = price_t * tick
            side = "BUY" if side_b == 0x42 else "SELL"
            _orders += 1

            if _orders % LOG_EVERY_N == 0:
                log.info(
                    f"ORDER #{_orders}  sym={sym_id}  {side}"
                    f"  price=${price_usd:.4f}  qty={qty}"
                    f"  id={order_id:#018x}"
                )

            entry = {
                "price_usd": price_usd,
                "side":      side,
                "symbol_id": sym_id,
                "qty":       qty,
            }

            with _lock:
                _pending[order_id] = entry
                # Immediate fill check (book may already have crossed)
                if order_id not in _cancelled and _price_crosses(entry):
                    _do_fill(order_id)
                    continue   # filled — no timer needed

            # Schedule fill-window timeout
            t = threading.Timer(window_ms / 1000.0, _fill_timeout, args=[order_id])
            t.daemon = True
            t.start()

        # ── Cancel (0x58 'X') ────────────────────────────────────────────
        elif msg_type == 0x58 and len(data) >= 9:
            order_id = struct.unpack_from(">Q", data, 1)[0]
            _cancels += 1
            with _lock:
                _cancelled.add(order_id)
                if order_id in _pending:
                    del _pending[order_id]
            _send_ack(order_id, ACK_CANCELLED, 0)
            log.info(f"CANCEL #{_cancels}  id={order_id:#018x}")

        else:
            log.warning(f"Unknown msg_type=0x{msg_type:02X}  len={len(data)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Realistic FPGA OUCH ACK simulator — fills only if price crosses"
    )
    parser.add_argument(
        "--window", type=int, default=FILL_WINDOW_MS,
        help=f"Fill window in ms: cancel unfilled orders after this long (default {FILL_WINDOW_MS})"
    )
    args = parser.parse_args()

    threading.Thread(target=_run_ws,       daemon=True).start()
    threading.Thread(target=_pnl_summary,  daemon=True).start()

    try:
        _ouch_loop(args.window)
    except KeyboardInterrupt:
        pass

    log.info(f"Stopped.  orders={_orders}  cancels={_cancels}")
    with _lock:
        total = sum(_pnl.values())
        for sid, (name, _) in _ID_TO_CFG.items():
            fr = _fills[sid] / max(1, _fills[sid] + _no_fills[sid])
            log.info(
                f"  {name.upper():>8}  pnl=${_pnl[sid]:+.4f}"
                f"  fills={_fills[sid]}  no-fill={_no_fills[sid]}"
                f"  fill-rate={fr:.0%}"
            )
        log.info(f"  TOTAL pnl=${total:+.4f}")


if __name__ == "__main__":
    main()
