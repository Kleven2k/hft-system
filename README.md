# HFT System

An FPGA-based high-frequency trading platform built on the Digilent Nexys Video (Xilinx Artix-7 XC7A200T).
Built for learning — market microstructure, FPGA engineering, and network protocols.

## Architecture

```
Ethernet RX → market_data_parser → order_book → strategy → order_engine → ouch_encoder → Ethernet TX
                                                                ↑
                                                   UART config (price_base, quote_offset, risk)
```

- **125 MHz** system clock, WNS = +0.001 ns
- **4 simultaneous symbols** (AVAX/LINK/AAVE/INJ)
- **~64 ns** market data → order out (8 pipeline stages)
- Full pre-trade risk: kill switch, fat-finger, position limits, inventory skew, EMA filter

## Repository Layout

```
fpga/           FPGA RTL (SystemVerilog), testbenches, build scripts
software/       Host software: feed bridge, ACK simulators, monitoring tools
research/       Data collection, backtesting, market monitors, Rust matching engine
docs/           Project overview, protocol specs, architecture decisions
```

## Quick Start

```bash
# Activate Python environment
.\research\.venv\Scripts\activate

# Build FPGA bitstream
& "C:\Xilinx\Vivado\2024.2\bin\vivado.bat" -mode tcl -source fpga/scripts/build.tcl

# Run live system (3 terminals)
python software/feed_bridge.py                    # Binance → FPGA market data
python software/ack_simulator_realistic.py        # Realistic fill simulation
python software/monitor.py                        # Live P&L telemetry

# DEX/CEX spread monitor
python research/monitors/dex_monitor.py --rpc https://avalanche-c-chain-rpc.publicnode.com

# Backtest
python research/backtest/backtest.py

# Run unit tests
cd fpga/tb/strategy && python runner_strategy.py
```

## Strategies

| Strategy | Status | Notes |
|----------|--------|-------|
| CEX market making (crypto) | Simulation-tested | Needs colocation for live profitability |
| DEX/CEX arbitrage (Avalanche) | Data collection | Monitor running, evaluating opportunity |
| NASDAQ stock HFT | In design | ITCH 5.0 spec in `docs/specs/`, next focus |

## Documentation

See [docs/OVERVIEW.md](docs/OVERVIEW.md) for full system description, phase history, and roadmap.
