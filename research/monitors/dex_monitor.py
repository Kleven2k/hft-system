#!/usr/bin/env python3
"""
dex_monitor.py — DEX/CEX price spread monitor for AVAX/USDC

CEX: Binance AVAXUSDT bookTicker WebSocket (real-time bid/ask)
DEX: Trader Joe V1 WAVAX/USDC.e pool on Avalanche C-Chain (per-block)

What this does:
  - Monitors the price gap between Binance and the on-chain DEX
  - Logs every observation to CSV for research
  - Shows live stats: current spread, % of time above fee threshold,
    theoretical P&L if we had traded every profitable divergence

Profitable arbitrage condition:
  abs(dex_price - cex_mid) / cex_mid > FEE_THRESHOLD_PCT

Fees (round-trip):
  Trader Joe V1 swap:  0.30%
  Binance taker:       0.10%
  Total:              ~0.40%  (FEE_THRESHOLD_PCT default)

Output CSV: research/data/dex_cex_spread_<date>.csv
  timestamp_ns, cex_bid, cex_ask, cex_mid, dex_price,
  spread_usd, spread_pct, direction, above_threshold

Usage:
  pip install web3 websockets
  python research/dex_monitor.py
  python research/dex_monitor.py --rpc https://your-rpc-url
"""

import argparse
import asyncio
import csv
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets
from web3 import Web3

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AVALANCHE_RPC  = "https://api.avax.network/ext/bc/C/rpc"
BINANCE_WS     = "wss://stream.binance.com:9443/ws/avaxusdt@bookTicker"

# Trader Joe V1 factory — derives pool address programmatically
TJ_V1_FACTORY  = "0x9Ad6C38BE94206cA50bb0d90783181662f0Cfa10"
WAVAX_ADDR     = "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"   # 18 decimals
USDC_E_ADDR    = "0xA7D7079b0FEaD91F3e65f86E8915Cb59c1a4C664"   # 6 decimals

# Round-trip fee threshold for profitable arb (%)
# TJ swap 0.30% + Binance taker 0.10% = 0.40% minimum
FEE_THRESHOLD_PCT = 0.40

# How often to poll the DEX (seconds) — Avalanche block time ~2s
DEX_POLL_INTERVAL = 2.0

DATA_DIR = Path(__file__).parent.parent / "data"

# ---------------------------------------------------------------------------
# Minimal ABIs
# ---------------------------------------------------------------------------

FACTORY_ABI = [{
    "inputs": [
        {"name": "tokenA", "type": "address"},
        {"name": "tokenB", "type": "address"}
    ],
    "name": "getPair",
    "outputs": [{"name": "pair", "type": "address"}],
    "stateMutability": "view",
    "type": "function"
}]

PAIR_ABI = [
    {
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("dex_monitor")

# ---------------------------------------------------------------------------
# Shared state (written by DEX thread, read by CEX async loop)
# ---------------------------------------------------------------------------

_dex_price: float = 0.0
_dex_lock  = threading.Lock()


def _set_dex_price(p: float) -> None:
    global _dex_price
    with _dex_lock:
        _dex_price = p


def _get_dex_price() -> float:
    with _dex_lock:
        return _dex_price


# ---------------------------------------------------------------------------
# DEX price poller (background thread)
# ---------------------------------------------------------------------------

def _init_dex(rpc_url: str):
    """Connect to Avalanche, derive pool address, return (w3, pair_contract, wavax_is_token1)."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise RuntimeError(f"Cannot connect to Avalanche RPC: {rpc_url}")
    log.info(f"DEX   connected to Avalanche (chain_id={w3.eth.chain_id})")

    factory = w3.eth.contract(
        address=Web3.to_checksum_address(TJ_V1_FACTORY),
        abi=FACTORY_ABI
    )
    pair_addr = factory.functions.getPair(
        Web3.to_checksum_address(WAVAX_ADDR),
        Web3.to_checksum_address(USDC_E_ADDR)
    ).call()

    if pair_addr == "0x0000000000000000000000000000000000000000":
        raise RuntimeError("Pair not found — check factory address and token addresses")

    log.info(f"DEX   WAVAX/USDC.e pool: {pair_addr}")
    pair = w3.eth.contract(address=pair_addr, abi=PAIR_ABI)

    # Determine token order: token0 < token1 by address
    token0 = pair.functions.token0().call().lower()
    wavax_is_token1 = (token0 != WAVAX_ADDR.lower())
    log.info(f"DEX   token0={'USDC.e' if wavax_is_token1 else 'WAVAX'}  "
             f"token1={'WAVAX' if wavax_is_token1 else 'USDC.e'}")

    return w3, pair, wavax_is_token1


def _dex_poll_loop(rpc_url: str) -> None:
    """Background thread: poll DEX pool price every DEX_POLL_INTERVAL seconds."""
    try:
        w3, pair, wavax_is_token1 = _init_dex(rpc_url)
    except Exception as e:
        log.error(f"DEX   init failed: {e}")
        return

    consecutive_errors = 0
    while True:
        try:
            r0, r1, _ = pair.functions.getReserves().call()
            # price = USDC / WAVAX, accounting for decimals
            if wavax_is_token1:
                # token0=USDC.e (6 dec), token1=WAVAX (18 dec)
                price = (r0 / 1e6) / (r1 / 1e18)
            else:
                # token0=WAVAX (18 dec), token1=USDC.e (6 dec)
                price = (r1 / 1e6) / (r0 / 1e18)
            _set_dex_price(price)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors <= 3:
                log.warning(f"DEX   getReserves error: {e}")
        time.sleep(DEX_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Statistics tracker
# ---------------------------------------------------------------------------

class SpreadStats:
    def __init__(self):
        self.n          = 0
        self.n_above    = 0
        self.sum_spread = 0.0
        self.max_spread = 0.0
        self.theo_pnl   = 0.0   # $ if we traded 100 AVAX every crossing
        self.last_print = time.monotonic()

    def update(self, spread_pct: float, spread_usd: float) -> None:
        self.n          += 1
        self.sum_spread += abs(spread_pct)
        self.max_spread  = max(self.max_spread, abs(spread_pct))
        if abs(spread_pct) > FEE_THRESHOLD_PCT:
            self.n_above += 1
            # Theoretical: trade 100 AVAX, profit = |spread| - fees
            net_edge_pct = abs(spread_pct) - FEE_THRESHOLD_PCT
            self.theo_pnl += net_edge_pct / 100.0 * abs(spread_usd) / (abs(spread_pct)/100.0) * 100

    def print_summary(self, cex_mid: float, dex_price: float,
                      spread_usd: float, spread_pct: float, direction: str) -> None:
        now = time.monotonic()
        if now - self.last_print < 10.0:
            return
        self.last_print = now
        avg = self.sum_spread / self.n if self.n else 0
        pct_above = 100.0 * self.n_above / self.n if self.n else 0
        log.info(
            f"CEX=${cex_mid:.4f}  DEX=${dex_price:.4f}  "
            f"spread={spread_usd:+.4f} ({spread_pct:+.3f}%)  {direction}"
        )
        log.info(
            f"  n={self.n}  avg={avg:.3f}%  max={self.max_spread:.3f}%  "
            f"above_threshold={pct_above:.1f}%  theo_pnl=${self.theo_pnl:.2f}"
        )


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def _open_csv():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = DATA_DIR / f"dex_cex_spread_{date_str}.csv"
    exists = path.exists()
    fh = open(path, "a", newline="")
    writer = csv.writer(fh)
    if not exists:
        writer.writerow([
            "timestamp_ns", "cex_bid", "cex_ask", "cex_mid",
            "dex_price", "spread_usd", "spread_pct", "direction", "above_threshold"
        ])
    log.info(f"CSV   writing to {path}")
    return fh, writer


# ---------------------------------------------------------------------------
# Main CEX WebSocket loop
# ---------------------------------------------------------------------------

async def run(rpc_url: str) -> None:
    fh, writer = _open_csv()
    stats = SpreadStats()

    async for ws in websockets.connect(BINANCE_WS, ping_interval=20):
        try:
            log.info("CEX   Binance WebSocket connected")
            async for raw in ws:
                msg      = json.loads(raw)
                cex_bid  = float(msg["b"])
                cex_ask  = float(msg["a"])
                cex_mid  = (cex_bid + cex_ask) / 2.0
                dex_price = _get_dex_price()

                if dex_price == 0.0:
                    continue   # DEX not ready yet

                ts_ns       = time.time_ns()
                spread_usd  = dex_price - cex_mid
                spread_pct  = spread_usd / cex_mid * 100.0
                direction   = "BUY_CEX_SELL_DEX" if spread_usd > 0 else "BUY_DEX_SELL_CEX"
                above       = 1 if abs(spread_pct) > FEE_THRESHOLD_PCT else 0

                writer.writerow([
                    ts_ns, f"{cex_bid:.4f}", f"{cex_ask:.4f}", f"{cex_mid:.4f}",
                    f"{dex_price:.4f}", f"{spread_usd:.4f}",
                    f"{spread_pct:.4f}", direction, above
                ])
                fh.flush()

                stats.update(spread_pct, spread_usd)
                stats.print_summary(cex_mid, dex_price, spread_usd, spread_pct, direction)

        except websockets.ConnectionClosed:
            log.warning("CEX   disconnected — reconnecting in 2s")
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"CEX   error: {e} — reconnecting in 5s")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="DEX/CEX spread monitor")
    parser.add_argument("--rpc", default=AVALANCHE_RPC,
                        help=f"Avalanche RPC URL (default: {AVALANCHE_RPC})")
    args = parser.parse_args()

    log.info(f"DEX   RPC: {args.rpc}")
    log.info(f"DEX   fee threshold: {FEE_THRESHOLD_PCT}%")
    log.info(f"DEX   pool: Trader Joe V1 WAVAX/USDC.e on Avalanche")

    # Start DEX poller in background thread
    t = threading.Thread(target=_dex_poll_loop, args=(args.rpc,), daemon=True)
    t.start()

    # Run CEX WebSocket loop
    try:
        asyncio.run(run(args.rpc))
    except KeyboardInterrupt:
        log.info("Stopped.")
        stats_path = DATA_DIR / f"dex_cex_spread_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
        log.info(f"Data saved to {stats_path}")


if __name__ == "__main__":
    main()
