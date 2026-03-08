#!/usr/bin/env python3
"""
runner_bridge.py — cocotb runner for itch_msg_bridge tests
Uses direct iverilog + vvp invocation (same pattern as fpga/tb/eth/run_sim.py)

Run from: fpga/tb/bridge/
    python runner_bridge.py
    python runner_bridge.py test_single_quote_crosses
"""

import os
import sys
import subprocess
from pathlib import Path

REPO_ROOT   = Path(__file__).resolve().parents[3]
TB_DIR      = Path(__file__).resolve().parent
RTL_DIR     = REPO_ROOT / "fpga" / "rtl"
VENV        = REPO_ROOT / "research" / ".venv"

SOURCES = [
    RTL_DIR / "core"        / "hft_pkg.sv",
    RTL_DIR / "core"        / "hft_msg_pkg.sv",
    RTL_DIR / "market_data" / "itch_cdc_fifo.sv",
    RTL_DIR / "market_data" / "itch_msg_bridge.sv",
    TB_DIR                  / "itch_msg_bridge_tb_wrapper.sv",
]

def run(testcase=None):
    build_dir = TB_DIR / "sim_build_bridge"
    build_dir.mkdir(exist_ok=True)
    vvp_out = build_dir / "sim.vvp"

    # ---- Step 1: Compile ------------------------------------
    compile_cmd = [
        "iverilog",
        "-g2012",
        "-I", str(RTL_DIR / "core"),
        "-o", str(vvp_out),
        "-s", "itch_msg_bridge_tb_wrapper",
    ] + [str(s) for s in SOURCES]

    print("\n=== Compiling ===")
    result = subprocess.run(compile_cmd)
    if result.returncode != 0:
        print("COMPILE FAILED")
        sys.exit(1)

    # ---- Find base Python (has the DLL cocotb needs) --------
    libpython_path = subprocess.check_output(
        ["cocotb-config", "--libpython"], text=True
    ).strip()
    base_python_home = str(Path(libpython_path).parent)

    # ---- Step 2: Run ----------------------------------------
    env = os.environ.copy()
    env["PATH"] = base_python_home + os.pathsep + env.get("PATH", "")
    env.update({
        "COCOTB_TEST_MODULES": "test_itch_bridge",
        "COCOTB_TESTCASE":            testcase or "",
        "TOPLEVEL":            "itch_msg_bridge_tb_wrapper",
        "TOPLEVEL_LANG":       "verilog",
        "COCOTB_RESOLVE_X":    "ZEROS",
        "PYTHONPATH":          str(TB_DIR),
        "COCOTB_LOG_LEVEL":    "INFO",
        "COCOTB_RESULTS_FILE": str(build_dir / "results.xml"),
        "PYTHONHOME":          base_python_home,
        "PYGPI_PYTHON_BIN":          str(Path(sys.executable)),
    })

    run_cmd = [
        "vvp",
        "-M", str(VENV / "Lib/site-packages/cocotb/libs"),
        "-m", "cocotbvpi_icarus",
        str(vvp_out),
    ]

    print(f"\nTB_DIR:     {TB_DIR}")
    print(f"PYTHONHOME: {base_python_home}")
    print(f"Test file:  {(TB_DIR / 'test_itch_bridge.py').exists()}")

    print("\n=== Running simulation ===")
    result = subprocess.run(run_cmd, env=env)

    if result.returncode != 0:
        print("\nSIMULATION FAILED")
        sys.exit(1)
    else:
        print("\nSIMULATION PASSED")

if __name__ == "__main__":
    testcase = sys.argv[1] if len(sys.argv) > 1 else None
    run(testcase)