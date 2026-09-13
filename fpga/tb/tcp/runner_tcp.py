#!/usr/bin/env python3
"""
runner_tcp.py — Phase 27 cocotb testbench for tcp_engine + soup_session.

Usage:
    python runner_tcp.py [test_name]

Examples:
    python runner_tcp.py                    # run all 6 tests
    python runner_tcp.py test_tcp_handshake # run one test
"""

import os
import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TB_DIR    = Path(__file__).resolve().parent

RTL_CORE        = REPO_ROOT / "fpga" / "rtl" / "core"
RTL_ETH         = REPO_ROOT / "fpga" / "rtl" / "eth"
RTL_ORDER_ENTRY = REPO_ROOT / "fpga" / "rtl" / "order_entry"
THIRD_PARTY_AXIS = REPO_ROOT / "fpga" / "third_party" / "verilog-ethernet" / "lib" / "axis" / "rtl"

SOURCES = [
    # Packages
    str(RTL_CORE        / "hft_pkg.sv"),

    # TCP + session layer
    str(RTL_ETH         / "tcp_engine.sv"),
    str(RTL_ORDER_ENTRY / "soup_session.sv"),

    # Testbench wrapper
    str(TB_DIR          / "tcp_tb_wrapper.sv"),
]

SIM_BUILD = TB_DIR / "sim_build_tcp"
VVP_FILE  = SIM_BUILD / "sim.vvp"


def compile():
    print("\n=== Compiling Phase 27 TCP testbench ===\n")
    SIM_BUILD.mkdir(exist_ok=True)

    cmd = [
        "iverilog", "-g2012",
        f"-I{RTL_CORE}",
        f"-I{RTL_ORDER_ENTRY}",
        "-o", str(VVP_FILE),
        "-s", "tcp_tb_wrapper",
    ] + SOURCES

    print("Sources:")
    for s in SOURCES:
        print(f"  {s}")
    print()

    r = subprocess.run(cmd, cwd=REPO_ROOT)
    if r.returncode != 0:
        print("\nCOMPILE FAILED")
        sys.exit(1)
    print("\nCompile OK")


def run(testcase=None):
    print("\n=== Running Phase 27 TCP simulation ===")

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
    env["COCOTB_TEST_MODULES"] = "test_tcp"
    env["PATH"]                = base_python_home + os.pathsep + env.get("PATH", "")

    if testcase:
        env["COCOTB_TEST_FILTER"] = testcase
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
