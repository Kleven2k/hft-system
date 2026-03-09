#!/usr/bin/env python3
"""
runner_order_book.py
Compiles and runs order_book cocotb tests under Icarus Verilog.
Usage:
    python runner_order_book.py [test_name]
"""

import os
import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TB_DIR    = Path(__file__).resolve().parent
RTL_CORE  = REPO_ROOT / "fpga" / "rtl" / "core"
RTL_OB    = REPO_ROOT / "fpga" / "rtl" / "order_book"

SOURCES = [
    str(RTL_CORE  / "hft_pkg.sv"),
    str(RTL_OB    / "order_book.sv"),
    str(TB_DIR    / "order_book_tb_wrapper.sv"),
]

SIM_BUILD = TB_DIR / "sim_build_order_book"
VVP_FILE  = SIM_BUILD / "sim.vvp"

def compile():
    print("\n=== Compiling ===\n")
    SIM_BUILD.mkdir(exist_ok=True)

    cmd = [
        "iverilog", "-g2012",
        f"-I{RTL_CORE}",
        "-o", str(VVP_FILE),
        "-s", "order_book_tb_wrapper",
    ] + SOURCES

    r = subprocess.run(cmd, cwd=REPO_ROOT)
    if r.returncode != 0:
        print("COMPILE FAILED")
        sys.exit(1)


def run(testcase=None):
    print("\n=== Running simulation ===")

    import subprocess, shutil
    cocotb_config = shutil.which("cocotb-config")
    libpython = subprocess.check_output(
        [cocotb_config, "--libpython"], text=True
    ).strip()
    # libpython = e.g. C:\Python311\python311.dll — go up one level for PYTHONHOME
    base_python_home = str(Path(libpython).parent)

    venv_python = sys.executable
    cocotb_libs = Path(venv_python).parents[1] / "Lib" / "site-packages" / "cocotb" / "libs"

    env = os.environ.copy()
    env["PYTHONHOME"]          = base_python_home
    env["PYGPI_PYTHON_BIN"]    = venv_python
    env["PYTHONPATH"]          = str(TB_DIR)
    env["COCOTB_TEST_MODULES"] = "test_order_book"
    env["PATH"]                = base_python_home + os.pathsep + env.get("PATH", "")

    if testcase:
        env["COCOTB_TEST_FILTER"] = testcase

    print(f"TB_DIR:     {TB_DIR}")
    print(f"PYTHONHOME: {base_python_home}")

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