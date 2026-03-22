# HFT System — Project Overview

> Last updated: 2026-03-22
> Hardware: Digilent Nexys Video (Xilinx Artix-7 XC7A200T), 125 MHz system clock
> Goal: Build a real HFT system — FPGA market-making pipeline, validated strategies, path to live trading.

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
| `software/feed_bridge.py` | Bridges Binance WebSocket → FPGA market data feed. Sends UART price_base + quote_offset config at startup. |
| `software/ack_simulator.py` | Simple ACK simulator — always fills every order. Useful for smoke tests. |
| `software/monitor.py` | Reads UDP telemetry from FPGA: P&L, fill counts, position, order counts per slot. |
| `software/set_price_base.py` | UART tool: sends price_base calibration for a given slot. |
| `software/set_risk.py` | UART tool: configures fat-finger limit, kill switch. |

### 1.3 Research & Backtesting

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
- SSH: `fredrikpi@100.70.245.92`

### Data Available
| Dataset | Location | Size | Coverage |
|---------|----------|------|----------|
| Binance ticks (live) | Pi: `~/hft-system/research/data/` | ~90 MB/day | Ongoing |
| NASDAQ ITCH files | `research/data/nasdaq/*.gz` | 5–6 GB each | 3 dates (2019–2020) |
| ITCH BookTick CSVs | `research/data/nasdaq/*_ticks.csv` | ~50–100 MB each | AAPL/MSFT/AMD × 3 dates |
| Alpaca minute bars | `research/data/alpaca/*_bars_1Min_*.csv` | ~30 MB/symbol | 2024–2026 (2 years) |

---

## 4. Roadmap

### Validated and Ready to Implement
| # | Task | Notes |
|---|------|-------|
| Phase 25 | Latency measurement — timestamp counters at 4 pipeline stages | Baseline before optimisation |
| Phase 26 | End-to-end testbench — raw ITCH UDP in → OUCH bytes out | Safety net for all future changes |
| Phase 27 | OUCH 4.2 session layer (Login/Logout/Heartbeat/sequence numbers) | **Production gate** |
| Phase 28 | ITCH 5.0 replay tool — feed historical data to FPGA over UDP | Enables real backtest loop |
| Phase 29 | Matching engine simulator (Rust) — closes FPGA ↔ simulator loop | Full end-to-end validation |
| Phase 30 | Configurable symbol mapping via UART | Replace hardcoded slot routing |
| Phase 31 | Book depth alpha — order imbalance filter in strategy.sv | First real alpha signal |

### Research Queue
| Task | Status |
|------|--------|
| Oslo Børs / Euronext data feed access | Research needed |
| Euronext co-location pricing (LD4) | Research needed |
| Paper trading via Alpaca REST (live signal validation) | Ready to build |
| Port mean-reversion strategy to RTL | Ready to build |
| Collect more ITCH dates + symbols | 3 more files available on NASDAQ FTP |

---

## 5. Key Numbers

| Metric | Value |
|--------|-------|
| FPGA clock | 125 MHz (8 ns period) |
| Latest build WNS | +0.001 ns (timing met) |
| Pipeline latency (sim) | ~8 clock cycles (~64 ns) |
| Symbols supported (FPGA) | 4 (expandable) |
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

# Run feed bridge (live Binance → FPGA)
python software/feed_bridge.py

# Read FPGA telemetry
python software/monitor.py

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
