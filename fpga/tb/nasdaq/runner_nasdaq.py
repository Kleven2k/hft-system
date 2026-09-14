#!/usr/bin/env python3
"""
runner_nasdaq.py — Phase 31
Compiles and runs the NASDAQ ITCH replay test under Icarus Verilog.

Usage:
    python runner_nasdaq.py
    NASDAQ_TB_CSV=path/to/msft_ticks.csv python runner_nasdaq.py
    NASDAQ_TB_MAX_ROWS=500000 python runner_nasdaq.py
"""

import os
import sys
import subprocess
from pathlib import Path

REPO_ROOT    = Path(__file__).resolve().parents[3]
TB_DIR       = Path(__file__).resolve().parent
RTL_CORE     = REPO_ROOT / "fpga" / "rtl" / "core"
RTL_STRATEGY = REPO_ROOT / "fpga" / "rtl" / "strategy"

SOURCES = [
    str(RTL_CORE     / "hft_pkg.sv"),
    str(RTL_STRATEGY / "strategy.sv"),
    str(TB_DIR       / "nasdaq_tb_wrapper.sv"),
]

SIM_BUILD = TB_DIR / "sim_build_nasdaq"
VVP_FILE  = SIM_BUILD / "sim.vvp"


def compile():
    print("\n=== Compiling ===\n")
    SIM_BUILD.mkdir(exist_ok=True)

    cmd = [
        "iverilog", "-g2012",
        f"-I{RTL_CORE}",
        "-o", str(VVP_FILE),
        "-s", "nasdaq_tb_wrapper",
    ] + SOURCES

    r = subprocess.run(cmd, cwd=REPO_ROOT)
    if r.returncode != 0:
        print("COMPILE FAILED")
        sys.exit(1)


def run():
    print("\n=== Running simulation ===")

    import shutil
    cocotb_config = shutil.which("cocotb-config")
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
    env["COCOTB_TEST_MODULES"] = "test_nasdaq"
    env["PATH"]                = base_python_home + os.pathsep + env.get("PATH", "")

    print(f"TB_DIR:     {TB_DIR}")
    print(f"PYTHONHOME: {base_python_home}")
    if env.get("NASDAQ_TB_CSV"):
        print(f"CSV:        {env['NASDAQ_TB_CSV']}")
    print(f"MAX_ROWS:   {env.get('NASDAQ_TB_MAX_ROWS', '200000 (default)')}")

    cmd = [
        "vvp",
        "-M", str(cocotb_libs),
        "-m", "cocotbvpi_icarus",
        str(VVP_FILE),
    ]
    subprocess.run(cmd, env=env, cwd=TB_DIR)


if __name__ == "__main__":
    compile()
    run()
