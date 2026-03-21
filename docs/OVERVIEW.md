# HFT System — Project Overview

> Last updated: 2026-03-21
> Hardware: Digilent Nexys Video (Xilinx Artix-7 XC7A200T), 125 MHz system clock
> Goal: Learn networking, market microstructure, and FPGA engineering through a real HFT system.
> Live trading is not required — validated simulation and realistic backtests are the target.

---

## 1. What Has Been Built

### 1.1 FPGA RTL (Hardware)

The FPGA implements a complete market-making pipeline in hardware. Everything from Ethernet frame reception
to order transmission runs without CPU involvement.

```
Ethernet RX
    │
    └─► market_data_parser.sv   — Parses ITCH-like binary feed (UDP)
            │
            └─► symbol_router.sv         — Routes messages to correct slot (0–3)
                    │
                    └─► order_book.sv            — Per-slot price level tracking, best bid/ask
                            │
                            └─► strategy.sv              — Spread/skew/EMA/stale logic, generates orders
                                    │
                                    └─► order_engine.sv          — OMS: order table, risk, watchdog
                                            │
                                            └─► ouch_encoder.sv          — Serialises OUCH order frames
                                                    │
                                                    └─► eth_stack_wrapper.sv     — Ethernet TX (UDP)
```

**Completed features (hardware-validated on Nexys Video):**

| Phase | Feature | Status |
|-------|---------|--------|
| 11 | UART price_base config (runtime price calibration) | ✓ HW validated |
| 15 | OMS order table + watchdog (10-second safety timeout) | ✓ HW validated |
| 16 | Pre-trade risk: kill switch, fat-finger, position limits | ✓ HW validated |
| 17 | Inventory skew — FPGA adjusts quotes based on position | ✓ HW validated |
| 21A | Multi-symbol: 4 independent slots (AVAX/LINK/AAVE/INJ) | ✓ HW validated |
| 21B | P&L tracking via UDP telemetry | ✓ HW validated |
| 22 | Dynamic EMA spread filter (suppresses quoting in high-spread regimes) | ✓ HW validated |
| 23 | Timing closure — WNS positive after pipeline optimisations | ✓ WNS=+0.001ns |
| 24 | UART-configurable quote_offset per slot + software stop-loss | ✓ WNS=+0.001ns |

**Known gaps / not yet built:**

- `best_bid_qty` / `best_ask_qty` wired to order_book output but not yet consumed by strategy
- No UART readback path (TX is echo-loopback only)
- Symbol routing is identity-mapped (slot 0 = symbol 0, hardcoded)
- No end-to-end simulation testbench (tb/order_entry and tb/market_data are empty stubs)
- OUCH session layer is bare-frame only — no Login/Logout/Heartbeat/sequence numbers
- ARP uses broadcast MAC (functional but not production-grade)

### 1.2 Software

| File | Purpose |
|------|---------|
| `software/feed_bridge.py` | Bridges Binance WebSocket → FPGA market data feed. Sends UART price_base + quote_offset config at startup. Holds kill_switch ON until all 4 symbols are initialised. |
| `software/ack_simulator.py` | Simple ACK simulator — always fills every order. Useful for smoke tests. |
| `software/ack_simulator_realistic.py` | Realistic simulator — connects to live Binance book, only fills if price crosses. Tracks P&L per symbol. |
| `software/monitor.py` | Reads UDP telemetry from FPGA: P&L, fill counts, position, order counts per slot. |
| `software/set_price_base.py` | UART tool: sends price_base calibration for a given slot. |
| `software/set_risk.py` | UART tool: configures fat-finger limit, kill switch. |

### 1.3 Research Tools

| File / Folder | Purpose |
|---------------|---------|
| `research/collectors/data_collector.py` | Collects live Binance tick data to Parquet (designed to run on Raspberry Pi 24/7) |
| `research/backtest/backtest.py` | Backtests market-making strategy against collected tick data. Sweeps quote_offset parameter. |
| `research/monitors/dex_monitor.py` | Monitors price spread between Binance CEX and Trader Joe V1 DEX on Avalanche. Logs to CSV. |
| `research/collectors/download_binance_data.py` | Downloads historical Binance klines for offline analysis. |
| `research/market_engine/` | Rust-based market simulator (matching engine + replay harness). Produces fills.csv/market.csv. |
| `research/lob/` | Limit order book research: collects top-of-book snapshots, builds Parquet datasets. |
| `docs/specs/NQTVITCHspecification.pdf` | Official NASDAQ TotalView-ITCH 5.0 specification (already on hand). |

---

## 2. Trading Strategies

### Strategy A — CEX Crypto Market Making
**Status: Built and simulation-tested. Not profitable at home latency.**

Post quotes on both sides of the mid-price for AVAX/LINK/AAVE/INJ on Binance.
Earn the bid-ask spread when filled. Cancel and re-quote when price moves (stale detection).

**The economics at home internet:**
- Fill rate: ~1 fill per 3 hours at quote_offset=3 ticks (AVAX)
- Adverse selection: when you get filled, it's usually because price is moving against you
- Profitable market making requires colocation (microsecond latency) to out-react other MMs

**What this strategy needs to be viable:**
- Colocation at exchange data centre (NY4/NY5 for Binance)
- 10GbE NIC with kernel bypass (Solarflare XtremeScale, Mellanox ConnectX)
- Completed OUCH 4.2 session layer (Phase 27) for real exchange connectivity

**What it's good for:**
- Learning market microstructure (why spreads exist, what adverse selection means)
- Testing the full FPGA pipeline end-to-end
- Benchmarking latency (Phase 25)

---

### Strategy B — DEX/CEX Crypto Arbitrage
**Status: Monitor built (dex_monitor.py), data collection in progress.**

Monitor the price of AVAX between Binance (CEX) and Trader Joe V1 DEX on Avalanche.
When the gap exceeds round-trip fees (~0.40%), trade both legs simultaneously.

**Why this works at home:**
- DEX price updates once per Avalanche block (~2 seconds)
- The arbitrage window is seconds, not microseconds
- Home internet (50–100ms latency) is fast enough
- No colocation required

**Fee structure:**
- Trader Joe V1 swap: 0.30%
- Binance taker: 0.10%
- Break-even threshold: 0.40% spread

**Current data (2026-03-21, ~19:32 UTC):**
- Live spread: ~0.323% (just below threshold)
- Consistently directional: DEX price above CEX
- Need overnight data to see how often spread exceeds 0.40%

**What this strategy needs to execute:**
- Avalanche wallet + web3.py transaction signing
- DEX swap execution (Trader Joe V1 `swapExactTokensForTokens`)
- CEX order execution (Binance API)
- Own Avalanche node (eliminates public RPC latency)
- Capital: $10k notional → ~$50–100/day theoretical if spread >0.40% often enough

**The FPGA's role here:** None directly. This is a software-only strategy.

---

### Strategy C — NASDAQ Stock Exchange HFT
**Status: Design phase. ITCH 5.0 spec already on hand.**

Target real US equities via NASDAQ protocols:
- **ITCH 5.0**: Binary UDP multicast feed — the real protocol for market data
- **OUCH 4.x / SoupBinTCP**: Binary TCP protocol for order entry

This is the highest educational value path. Everything we've built maps directly to real production infrastructure.

**Why this is the right next step:**
- NASDAQ publishes free historical ITCH 5.0 data (full order book, every message, every stock)
- A realistic simulator can be built locally — no exchange access required
- OUCH 4.x is what we've already partially implemented
- The protocols are publicly documented (spec already in docs/)
- Maps directly to what HFT firms actually run

**The execution stack:**
```
Historical ITCH data (NASDAQ FTP, free)
    │
    └─► ITCH replay tool (Python/Rust) ──► FPGA (via UDP, same as today)
                                                │
                                            Order book reconstruction
                                                │
                                            Strategy (market making or stat-arb)
                                                │
                                            OUCH 4.x / SoupBinTCP
                                                │
                                            Matching engine simulator (Rust, local)
                                                │
                                            ACK back to FPGA
```

**What needs to be built:**
1. ITCH 5.0 parser (software replay + optionally FPGA native)
2. SoupBinTCP session layer (wraps OUCH for real exchange compatibility)
3. Matching engine simulator (Rust — foundation already exists in `research/market_engine/`)
4. ITCH replay → FPGA feed bridge

---

## 3. Roadmap

### Near-term: Consolidation (no new hardware builds)
These can be done without touching the FPGA RTL.

| # | Task | What you learn |
|---|------|---------------|
| R1 | Run `dex_monitor.py` overnight | Whether DEX/CEX arb opportunity is real |
| R2 | Analyse dex_monitor CSV: spread distribution, % above threshold | Data-driven strategy validation |
| R3 | Set up Raspberry Pi running `data_collector.py` as systemd service | Linux services, continuous data collection |

### Phase 25 — Latency Measurement
Add 64-bit timestamp counters at 4 pipeline stages. Report min/avg/max in telemetry.
- Target: market data in → order out < 1 µs
- **Why:** Baseline measurement before any further optimisation. You can't improve what you don't measure.

### Phase 26 — End-to-End System Testbench
Fill the empty stubs in `tb/order_entry/` and `tb/market_data/`.
A single cocotb test drives raw UDP ITCH bytes in, asserts correct OUCH bytes out.
- **Why:** Safety net. Every phase so far has been tested at unit level only.

### Phase 27 — OUCH 4.2 Session Layer (Production Gate)
Replace bare-frame encoder with proper NASDAQ OUCH 4.2:
- Login / Logout messages
- Sequence numbers on every outbound message
- Heartbeat every 1s when idle
- Server heartbeat timeout watchdog
- Session FSM: `LOGGED_OUT → LOGGING_IN → ACTIVE → LOGGING_OUT`

**This is the production gate.** After Phase 27, the FPGA can talk to any OUCH-compatible simulator
or test server, including NASDAQ's own test environment (for registered firms).

### Phase 28 — ITCH 5.0 Replay Tool
Build a tool that reads real NASDAQ historical ITCH data and replays it to the FPGA over UDP.
- Parses the binary ITCH 5.0 format (spec in `docs/specs/NQTVITCHspecification.pdf`)
- Translates to the FPGA's internal message format
- Controls replay speed (real-time, 10×, unlimited)
- **Why:** Lets you backtest against a real full trading day with microsecond-resolution data

### Phase 29 — Stock Matching Engine Simulator
Extend `research/market_engine/` (Rust) into a proper NASDAQ-style matching engine:
- Accepts OUCH orders from FPGA
- Matches against the replayed ITCH order book
- Sends back ACKs (fills, cancels, rejects)
- Tracks P&L, fill rate, adverse selection metrics
- **Why:** Complete the loop. ITCH replay in → FPGA → OUCH orders → matching engine → results.

### Phase 30 — Configurable Symbol Mapping (UART)
Replace hardcoded identity routing with a UART-programmable 4-entry lookup table.
Allows any 4 symbols from the ITCH feed to be assigned to the 4 FPGA slots at runtime.

### Phase 31 — Book Depth Alpha Signal
Wire `best_bid_qty` / `best_ask_qty` into strategy.
Add order imbalance filter: suppress BUY if bid_qty >> ask_qty (adverse selection indicator).
- **Why:** First step toward a real alpha signal beyond pure spread capture.

---

## 4. Technology Map

```
┌─────────────────────────────────────────────────────────┐
│                    Learning Objectives                   │
├──────────────────┬──────────────────┬───────────────────┤
│  Market Structure│  FPGA / HW       │   Networking      │
│                  │                  │                   │
│ - Bid/ask spread │ - SystemVerilog  │ - UDP/TCP sockets │
│ - Adverse select.│ - Timing closure │ - Ethernet frames │
│ - Order book     │ - CDC pipelines  │ - ITCH 5.0 (UDP)  │
│ - Inventory risk │ - Vivado tooling │ - OUCH/SoupBinTCP │
│ - EMA filters    │ - Cocotb testing │ - Kernel bypass   │
│ - DEX mechanics  │ - BRAM / LUT tradeoffs│- ARP/IP stack│
│ - MEV / arb      │ - WNS/TNS timing │ - WebSocket feeds │
└──────────────────┴──────────────────┴───────────────────┘
```

---

## 5. Key Numbers

| Metric | Value |
|--------|-------|
| FPGA clock | 125 MHz (8 ns period) |
| Latest build WNS | +0.001 ns (timing met) |
| Pipeline: market data → order out | ~8 clock cycles (~64 ns) measured in sim |
| Symbols supported | 4 (expandable) |
| UART baud | 115200 |
| DEX poll interval | 2 s (Avalanche block time) |
| AVAX fill rate (sim, offset=3) | ~1 fill / 3 hours |
| Fee break-even (DEX/CEX arb) | 0.40% spread |

---

## 6. Quick Reference

```bash
# Activate Python environment
.\research\.venv\Scripts\activate

# Build FPGA
& "C:\Xilinx\Vivado\2024.2\bin\vivado.bat" -mode tcl -source fpga/scripts/build.tcl

# Run unit tests (strategy)
cd fpga/tb/strategy && python runner_strategy.py

# Run feed bridge (live Binance → FPGA)
python software/feed_bridge.py

# Run realistic ACK simulator
python software/ack_simulator_realistic.py

# Read FPGA telemetry
python software/monitor.py

# Run DEX/CEX spread monitor
python research/monitors/dex_monitor.py --rpc https://avalanche-c-chain-rpc.publicnode.com

# Set price base via UART (slot 0, value 2400)
python software/set_price_base.py --slot 0 --value 2400
```
