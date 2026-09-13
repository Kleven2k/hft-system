# HFT System — Project Overview

> Last updated: 2026-09-13
> Hardware: Digilent Nexys Video (Xilinx Artix-7 XC7A200T), 125 MHz system clock
> Goal: Build a real HFT system — FPGA market-making pipeline, validated strategies, path to live trading.

---

## 1. What Has Been Built

### 1.1 FPGA RTL (Hardware)

The FPGA implements a complete market-making pipeline in hardware. Everything from Ethernet frame reception
to order transmission runs without CPU involvement.

**Paper-trading path (UDP, always active):**
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
                                            ├─► ouch_encoder.sv          — Serialises OUCH 4.2 order frames
                                            │           │
                                            │           ├─► soup_session.sv     — SoupBinTCP Login/HB/framing
                                            │           │           │
                                            │           │           └─► tcp_engine.sv    — ARP/TCP handshake/TX/RX
                                            │           │
                                            │           └─► eth_stack_wrapper.sv — UDP telemetry + paper-mode ACK
                                            │
                                            └─► mac_tx_arbiter.sv     — 2:1 TX arbiter (TCP priority over UDP)
```

Both TCP (live) and UDP (paper) paths are active simultaneously:
- **Paper mode:** FPGA → UDP → `ack_simulator.py` → UDP ACK → FPGA
- **Live mode:** FPGA → TCP → exchange → execution reports → TCP ACK → FPGA

**Completed features (hardware-validated on Nexys Video unless noted):**

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
| 25 | Latency measurement — timestamp counters at 4 pipeline stages via telemetry | ✓ WNS=+0.006ns, HW validated |
| 26 | End-to-end cocotb testbench — raw ITCH UDP in → OUCH bytes out (6 tests) | ✓ 6/6 pass, WNS=+0.018ns |
| 27 | Full FPGA TCP: `tcp_engine` + `soup_session` — direct order submission | ✓ HW validated — Login Accepted, heartbeats confirmed |
| 28 | Binance live trading pipeline + queue-position backtest sim | ✓ HW validated — 10 orders/sec BUY/SELL on AAVE+INJ |

**Known gaps / not yet built:**

- `best_bid_qty` / `best_ask_qty` wired to order_book output but not yet consumed by strategy
- No UART readback path (TX is echo-loopback only)
- Symbol routing is identity-mapped (slot 0 = symbol 0, hardcoded)
- No TCP retransmit timer — LAN is assumed reliable for initial deployment
- ARP uses broadcast MAC (functional but not production-grade)

### 1.2 Phase 27 — Full FPGA TCP (SoupBinTCP / OUCH 4.2)

Phase 27 eliminates software from the order critical path. The FPGA opens a TCP connection to the exchange
and sends OUCH 4.2 orders directly.

**New RTL files:**

| File | Purpose |
|------|---------|
| `fpga/rtl/eth/tcp_engine.sv` | Minimal TCP client: ARP → 3-way handshake → PSH+ACK data TX, pure-ACK RX. Single connection, no retransmit (LAN assumed reliable). All timing-safe: no `always_comb`, checksums computed in one `S_TX_PREP` cycle from precomputed parameter constants. |
| `fpga/rtl/eth/mac_tx_arbiter.sv` | 2:1 MAC TX arbiter — `tcp_engine` (priority 0) vs UDP telemetry/paper-ACK (priority 1). |
| `fpga/rtl/order_entry/soup_session.sv` | SoupBinTCP session layer on top of `tcp_engine`. Sends Login Request on connect, heartbeat every HB_CYC cycles, wraps OUCH frames in 'U' unsequenced data, parses inbound 'A'/'S' execution reports. |

**Configuration (parameters):**
- Exchange IP/port: `tcp_engine` parameters `SERVER_IP`, `SERVER_PORT`
- Credentials: `soup_session` parameters `SOUP_USER`, `SOUP_PASS`
- Heartbeat interval: `soup_session` parameter `HB_CYC` (default 125,000,000 = 1s at 125 MHz)

**Testbench:** `fpga/tb/tcp/` — 6 cocotb tests (ARP, TCP handshake, Login, Order submit, Exec report, Heartbeat).
Run: `python fpga/tb/tcp/runner_tcp.py`

**Hardware validation (2026-04-23):** fixed by bypassing the arbiter (tcp_engine wired direct to MAC).
Login Accepted and heartbeat exchange confirmed on real hardware against `software/exchange_sim.py`.

### 1.2b Phase 28 — Binance Live Trading Pipeline

Closes the loop from a live market feed to a simulated exchange fill, entirely through the FPGA.

```
Binance WebSocket → feed_bridge.py → FPGA (UDP market data)
                                          → strategy → order_engine → ouch_encoder
                                              → soup_session → tcp_engine → TCP → exchange_sim.py
```

**New software:**

| File | Purpose |
|------|---------|
| `software/exchange_sim.py` | Full TCP/SoupBinTCP/OUCH 4.2 exchange simulator — accepts the FPGA's live connection, login, and orders; sends back execution reports. Used to validate Phase 27's TCP path without a real exchange. |
| `software/ouch_session.py` | OUCH session client library shared by exchange_sim and other tools. |

**Fixes during bring-up:**
- Token decode bug in `soup_session.sv` — `token_to_id` nibble order was reversed, causing order tokens to mismatch on exec reports.
- `strategy.sv` `QUOTE_OFFSET` moved from a compile-time parameter to a UART-configurable runtime `quote_offset[]` per slot, so live parameters can be tuned without a rebuild. This added a second serial subtract to the inventory-skew critical path, fixed by an extra pipeline stage.

**Validated (2026-04-25):** full pipeline running continuously — 10 orders/sec BUY/SELL on AAVE and INJ,
with stale orders correctly cancelled and fills correctly executed against `exchange_sim.py`.

**Backtest addition:** `research/backtest/backtest.py` now supports `--fill-prob` to simulate queue position
(a touch at the quoted price fills probabilistically rather than immediately, approximating resting behind
other orders). Realistic AVAX estimate at `fill_prob=0.2`: ~$18/day.

### 1.3 Software

| File | Purpose |
|------|---------|
| `software/feed_bridge.py` | Bridges Binance WebSocket → FPGA market data feed. Sends UART price_base + quote_offset config at startup. |
| `software/ack_simulator.py` | Simple ACK simulator — always fills every order. Useful for smoke tests (paper mode). |
| `software/monitor.py` | Reads UDP telemetry from FPGA: P&L, fill counts, position, order counts per slot, pipeline latency. |
| `software/set_price_base.py` | UART tool: sends price_base calibration for a given slot. |
| `software/set_risk.py` | UART tool: configures fat-finger limit, kill switch. |
| `software/exchange_sim.py` | Full TCP/SoupBinTCP/OUCH 4.2 exchange simulator for validating the live order path (Phase 27/28). |
| `software/ouch_session.py` | OUCH session client library shared across tools. |

### 1.4 Research & Backtesting

#### Data Collection
| File | Purpose |
|------|---------|
| `research/collectors/data_collector.py` | Streams Binance WebSocket tick data to CSV — runs 24/7 on Raspberry Pi as systemd service |
| `research/collectors/alpaca_history.py` | Downloads up to 2 years of minute bars from Alpaca (AAPL/MSFT/AMD/NVDA/QQQ) |
| `research/collectors/alpaca_collector.py` | Live Alpaca WebSocket collector (requires live account) |
| `research/monitors/dex_monitor.py` | Monitors DEX/CEX spread between Binance and Trader Joe V1 on Avalanche |

#### NASDAQ ITCH Tools (Rust)
| File | Purpose |
|------|---------|
| `research/nasdaq/itch_rs/` | Rust ITCH 5.0 parser + order book reconstructor. Parses 423M messages in ~2 min (vs 10+ min in Python) |
| `research/nasdaq/itch_parser.py` | Python ITCH 5.0 parser (reference implementation) |
| `research/nasdaq/order_book.py` | Python order book reconstructor — BookTick stream |
| `research/nasdaq/download.py` | Downloads historical ITCH files from NASDAQ (5–6 GB each) |
| `research/nasdaq/show_scan.py` | Pretty-prints symbol scanner output |

**Rust tool usage:**
```bash
# Parse one symbol → BookTick CSV (fast path for backtests)
.\research\nasdaq\itch_rs\target\release\itch_parser.exe --file data.gz --symbol AAPL --out aapl_ticks.csv

# Scan all 8900 symbols, rank by MM suitability
.\research\nasdaq\itch_rs\target\release\itch_parser.exe --file data.gz --scan --top 50 --out scan.csv
```

#### Backtests
| File | Purpose |
|------|---------|
| `research/backtest/nasdaq_mm_backtest.py` | Market-making backtest on ITCH tick data. Supports fast CSV path from Rust parser. |
| `research/backtest/multi_sweep.py` | Multi-symbol, multi-date sweep (AAPL/MSFT/AMD across 3 dates) |
| `research/backtest/bars_backtest.py` | Mean-reversion + momentum backtest on Alpaca minute bars (2yr recent data) |
| `research/backtest/dex_arb_backtest.py` | DEX/CEX arbitrage backtest on collected spread data |

---

## 2. Trading Strategies

### Strategy A — CEX Crypto Market Making
**Status: Built and simulation-tested. Not profitable at home latency.**

Post quotes on both sides of the mid-price for AVAX/LINK/AAVE/INJ on Binance.
Earn the bid-ask spread when filled. Cancel and re-quote when price moves (stale detection).

**Why it's not viable from home:** Adverse selection eats all profit. Need colocation.

---

### Strategy B — DEX/CEX Crypto Arbitrage
**Status: Monitor built, backtest complete. Marginally unprofitable in current market.**

Monitor price spread between Binance (CEX) and Trader Joe V1 DEX on Avalanche.
Trade both legs when gap exceeds ~0.51% breakeven (fees + price impact + gas).

**Current finding:** Max observed spread (0.428%) is below breakeven (0.510%). Only viable during high volatility events.

---

### Strategy C — NASDAQ Stock Market Making (Mean Reversion)
**Status: Backtested and validated. Profitable on real data. Next: FPGA implementation.**

Quote passively at best bid/ask. Cancel quickly when mid moves (stale detection).
Fade short-term price moves — profit from mean reversion.

**Backtest results (real ITCH data, 3 dates, 3 symbols):**

| Symbol | Avg P&L/hr | Dates Profitable | Best Params |
|--------|-----------|-----------------|-------------|
| AAPL | $+253/hr | 3/3 | offset=0, stale=2 |
| MSFT | $+119/hr | 3/3 | offset=0, stale=2 |
| AMD  | $+86/hr  | 3/3 | offset=0, stale=2 |

**2-year minute bar backtest (Alpaca, 2024–2026):**

| Symbol | Strategy | Best P&L (2yr) | Sharpe |
|--------|----------|---------------|--------|
| NVDA | Mean reversion | $+5,444 | 11.0 |
| MSFT | Mean reversion | $+3,590 | 58.3 |
| AMD  | Mean reversion | $+2,770 | 7.0 |
| AAPL | Mean reversion | $+2,219 | 7.2 |
| QQQ  | Mean reversion | $+1,164 | 24.4 |

**Momentum (EMA crossover): consistently unprofitable on all symbols — do not use.**

**Key insight:** Mean reversion IS the market-making strategy. Fade short moves, capture spread. Works at both tick level (ITCH) and minute level (bars). The FPGA already implements this.

---

### Strategy D — Oslo Børs / Euronext Market Making
**Status: Research phase.**

Target European equities via Oslo Børs (Euronext group).
- Oslo Børs uses NASDAQ ITCH protocol — FPGA parser works with minor modifications
- Matching engine located in Basildon, UK (LD4) — Norway has 3–4× latency advantage over US firms
- End goal: co-locate FPGA in LD4 data centre once profitable

---

## 3. Infrastructure

### Raspberry Pi (Data Collector)
- Running `hft-collector` systemd service 24/7
- Collecting Binance WebSocket ticks: AVAX, LINK, AAVE, INJ (~18 ticks/sec each)
- ~90 MB/day, 6 GB free on SD card
- Accessible via Tailscale VPN (`100.70.245.92`) from anywhere
- SSH: `fredrikpi@100.70.245.92` (passwordless via SSH key)

**Checking Pi services/processes:**
```bash
# Check the tick collector
ssh fredrikpi@100.70.245.92 "sudo systemctl status hft-collector"

# Check the DEX monitor
ssh fredrikpi@100.70.245.92 "sudo systemctl status hft-dex-monitor"

# List all hft-* services at once
ssh fredrikpi@100.70.245.92 "systemctl list-units 'hft-*' --all"

# Live logs (Ctrl+C to stop)
ssh fredrikpi@100.70.245.92 "journalctl -u hft-collector -f"

# Raw process list
ssh fredrikpi@100.70.245.92 "ps aux | grep python"
```

**Syncing collected data to laptop:**
```bash
bash research/collectors/sync_from_pi.sh
```
Pulls latest CSVs from `~/hft-system/research/data/` on the Pi into the local `research/data/` folder via `scp`, then deletes the Pi-side copies of files from previous days (today's file is kept — the collector still has it open for writing). Run this periodically to keep the Pi's ~15GB SD card from filling up (~90MB/day).

**Disk space backstop:** a daily cron job on the Pi (4 AM) deletes any CSV older than 14 days regardless of sync status, as a safety net if syncing is forgotten:
```
0 4 * * * find /home/fredrikpi/hft-system/research/data -name "*.csv" -mtime +14 -delete
```

### Data Available
| Dataset | Location | Size | Coverage |
|---------|----------|------|----------|
| Binance ticks (live) | Pi: `~/hft-system/research/data/` | ~90 MB/day | Ongoing |
| NASDAQ ITCH files | `research/data/nasdaq/*.gz` | 5–6 GB each | 3 dates (2019–2020) |
| ITCH BookTick CSVs | `research/data/nasdaq/*_ticks.csv` | ~50–100 MB each | AAPL/MSFT/AMD × 3 dates |
| Alpaca minute bars | `research/data/alpaca/*_bars_1Min_*.csv` | ~30 MB/symbol | 2024–2026 (2 years) |

---

## 4. Roadmap

### Completed
| Phase | Task | Notes |
|-------|------|-------|
| 25 | Latency measurement — timestamp counters at 4 pipeline stages | WNS=+0.006ns, HW validated |
| 26 | End-to-end testbench — raw ITCH UDP in → OUCH bytes out | 6/6 cocotb tests pass, WNS=+0.018ns |
| 27 | Full FPGA TCP/SoupBinTCP/OUCH 4.2 order submission | HW validated — Login Accepted, heartbeats confirmed |
| 28 | Binance live trading pipeline + queue-position backtest sim | HW validated — 10 orders/sec BUY/SELL on AAVE+INJ |

### Next Up
| # | Task | Notes |
|---|------|-------|
| Phase 29 | TCP retransmit timer | Low priority — LAN is reliable; needed before WAN deployment |
| Phase 29 | ITCH 5.0 replay tool — feed historical data to FPGA over UDP | Enables real backtest loop |
| Phase 30 | Matching engine simulator (Rust) — closes FPGA ↔ simulator loop | Full end-to-end validation |
| Phase 31 | Configurable symbol mapping via UART | Replace hardcoded slot routing |
| Phase 32 | Book depth alpha — order imbalance filter in strategy.sv | First real alpha signal |

### Research Queue
| Task | Status |
|------|--------|
| Oslo Børs / Euronext data feed access | Research needed |
| Euronext co-location pricing (LD4) | Research needed |
| Paper trading via Alpaca REST (live signal validation) | Ready to build |
| Port mean-reversion strategy to RTL | Ready to build |
| Collect more ITCH dates + symbols | 3 more files available on NASDAQ FTP |
| Live exchange validation (real venue, not exchange_sim) | Not yet attempted |

---

## 5. Key Numbers

| Metric | Value |
|--------|-------|
| FPGA clock | 125 MHz (8 ns period) |
| Latest build WNS | +0.018 ns (Phase 26, timing met) |
| Pipeline latency (sim) | ~8 clock cycles (~64 ns) |
| Symbols supported (FPGA) | 4 (expandable) |
| Testbench coverage | 6/6 strategy + 6/6 TCP + 6/6 e2e tests |
| ITCH parse speed (Rust) | 423M messages in ~2 min |
| ITCH parse speed (Python) | 423M messages in ~15+ min |
| AAPL book ticks per day | ~2M |
| Best MM result (ITCH) | AAPL $499/hr (Jan 2020) |
| Best MM result (avg, 3 dates) | AAPL $253/hr |
| Best bar backtest (2yr) | NVDA mean-reversion $5,444 Sharpe=11 |
| Pi tick collection rate | ~18 ticks/sec per symbol |
| Tailscale Pi IP | 100.70.245.92 |

---

## 6. Quick Reference

```bash
# Activate Python environment
.\.venv\Scripts\activate

# Build FPGA
& "C:\Xilinx\Vivado\2024.2\bin\vivado.bat" -mode tcl -source fpga/scripts/build.tcl

# Run strategy unit tests
cd fpga/tb/strategy && python runner_strategy.py

# Run Phase 26 end-to-end tests (ITCH→OUCH)
python fpga/tb/strategy/runner_strategy.py

# Run Phase 27 TCP/SoupBinTCP tests
python fpga/tb/tcp/runner_tcp.py

# Run single TCP test
python fpga/tb/tcp/runner_tcp.py test_tcp_handshake

# Run feed bridge (live Binance → FPGA)
python software/feed_bridge.py

# Read FPGA telemetry
python software/monitor.py

# Paper-mode ACK simulator
python software/ack_simulator.py

# Parse ITCH file (Rust, fast)
.\research\nasdaq\itch_rs\target\release\itch_parser.exe --file research\data\nasdaq\01302020.NASDAQ_ITCH50.gz --symbol AAPL --out research\data\nasdaq\aapl_ticks.csv

# Run NASDAQ MM backtest (fast path)
python research/backtest/nasdaq_mm_backtest.py --ticks-file research\data\nasdaq\aapl_20200130_ticks.csv --symbol AAPL --sweep

# Run multi-symbol backtest sweep
python research/backtest/multi_sweep.py

# Run bars backtest (2yr Alpaca data)
python research/backtest/bars_backtest.py --sweep

# Download 2yr Alpaca minute bars
python research/collectors/alpaca_history.py --days 730 --timeframe 1Min

# Scan all NASDAQ symbols for MM candidates
.\research\nasdaq\itch_rs\target\release\itch_parser.exe --file research\data\nasdaq\01302020.NASDAQ_ITCH50.gz --scan --top 50 --out research\data\nasdaq\scan.csv
python research/nasdaq/show_scan.py research\data\nasdaq\scan.csv

# SSH to Raspberry Pi (from anywhere via Tailscale)
ssh fredrikpi@100.70.245.92
```
