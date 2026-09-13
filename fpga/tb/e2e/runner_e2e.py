#!/usr/bin/env python3
"""
runner_e2e.py
Compiles and runs Phase 26 end-to-end cocotb tests under Icarus Verilog.

Pipeline under test:
  UDP RX bytes (rxc_clk)
    → market_data_parser
      → itch_msg_bridge (CDC)
        → symbol_router
          → order_book × 4
            → order_engine (strategy + ouch_encoder)
  → UDP TX bytes (rxc_clk) = OUCH new-order / cancel frames

Usage:
    python runner_e2e.py [test_name]
"""

import os
import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TB_DIR    = Path(__file__).resolve().parent

RTL_CORE         = REPO_ROOT / "fpga" / "rtl" / "core"
RTL_MARKET_DATA  = REPO_ROOT / "fpga" / "rtl" / "market_data"
RTL_ORDER_BOOK   = REPO_ROOT / "fpga" / "rtl" / "order_book"
RTL_STRATEGY     = REPO_ROOT / "fpga" / "rtl" / "strategy"
RTL_ORDER_ENTRY  = REPO_ROOT / "fpga" / "rtl" / "order_entry"
RTL_ETH          = REPO_ROOT / "fpga" / "rtl" / "eth"
THIRD_PARTY_AXIS = REPO_ROOT / "fpga" / "third_party" / "verilog-ethernet" / "lib" / "axis" / "rtl"

SOURCES = [
    # Packages first
    str(RTL_CORE        / "hft_pkg.sv"),
    str(RTL_CORE        / "hft_msg_pkg.sv"),

    # Core modules
    str(RTL_CORE        / "latency_monitor.sv"),

    # Market data pipeline
    str(RTL_MARKET_DATA / "market_data_parser.sv"),
    str(RTL_MARKET_DATA / "itch_cdc_fifo.sv"),
    str(RTL_MARKET_DATA / "itch_msg_bridge.sv"),
    str(RTL_MARKET_DATA / "symbol_router.sv"),

    # Order book
    str(RTL_ORDER_BOOK  / "order_book.sv"),

    # Strategy
    str(RTL_STRATEGY    / "strategy.sv"),

    # Third-party AXIS CDC FIFO (used by order_cdc_bridge)
    str(THIRD_PARTY_AXIS / "axis_async_fifo.v"),

    # Order entry
    str(RTL_ORDER_ENTRY / "order_cdc_bridge.sv"),
    str(RTL_ORDER_ENTRY / "ouch_encoder.sv"),
    str(RTL_ORDER_ENTRY / "order_engine.sv"),

    # Telemetry (instantiated inside order_engine)
    str(RTL_ETH         / "telemetry_tx.sv"),

    # Top-level testbench wrapper
    str(TB_DIR          / "e2e_tb_wrapper.sv"),
]

SIM_BUILD = TB_DIR / "sim_build_e2e"
VVP_FILE  = SIM_BUILD / "sim.vvp"


def compile():
    print("\n=== Compiling e2e testbench ===\n")
    SIM_BUILD.mkdir(exist_ok=True)

    cmd = [
        "iverilog", "-g2012",
        f"-I{RTL_CORE}",
        f"-I{RTL_MARKET_DATA}",
        f"-I{RTL_ORDER_ENTRY}",
        "-o", str(VVP_FILE),
        "-s", "e2e_tb_wrapper",
    ] + SOURCES

    print("iverilog sources:")
    for s in SOURCES:
        print(f"  {s}")

    r = subprocess.run(cmd, cwd=REPO_ROOT)
    if r.returncode != 0:
        print("\nCOMPILE FAILED")
        sys.exit(1)
    print("\nCompile OK")


def run(testcase=None):
    print("\n=== Running e2e simulation ===")

    import shutil
    cocotb_config = shutil.which("cocotb-config")
    if not cocotb_config:
        print("ERROR: cocotb-config not found — activate your venv first")
        sys.exit(1)

    libpython = subprocess.check_output(
        [cocotb_config, "--libpython"], text=True
    ).strip()
    base_python_home = str(Path(libpython).parent)

    venv_python = sys.executable
    cocotb_libs = Path(venv_python).parents[1] / "Lib" / "site-packages" / "cocotb" / "libs"

    env = os.environ.copy()
    env["PYTHONHOME"]          = base_python_home
    env["PYGPI_PYTHON_BIN"]    = venv_python
    env["PYTHONPATH"]          = str(TB_DIR)
    env["COCOTB_TEST_MODULES"] = "test_e2e"
    env["PATH"]                = base_python_home + os.pathsep + env.get("PATH", "")

    if testcase:
        env["COCOTB_TEST_FILTER"] = testcase

    print(f"TB_DIR:     {TB_DIR}")
    print(f"PYTHONHOME: {base_python_home}")
    if testcase:
        print(f"Test filter: {testcase}")

    cmd = [
        "vvp",
        "-M", str(cocotb_libs),
        "-m", "cocotbvpi_icarus",
        str(VVP_FILE),
    ]
    subprocess.run(cmd, env=env, cwd=TB_DIR)


if __name__ == "__main__":
    testcase = sys.argv[1] if len(sys.argv) > 1 else None
    compile()
    run(testcase)
