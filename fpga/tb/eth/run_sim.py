#!/usr/bin/env python3
import os
import sys
import subprocess
from pathlib import Path

REPO_ROOT   = Path(__file__).resolve().parents[3]
TB_DIR      = Path(__file__).resolve().parent
RTL_DIR     = REPO_ROOT / "fpga" / "rtl"
THIRD_PARTY = REPO_ROOT / "fpga" / "third_party" / "verilog-ethernet"
VENV        = REPO_ROOT / "research" / ".venv"

VERILOG_ETH_RTL = list((THIRD_PARTY / "rtl").glob("*.v"))
AXIS_LIB_RTL    = list((THIRD_PARTY / "lib/axis/rtl").glob("*.v"))

SOURCES = [
    RTL_DIR / "core" / "hft_pkg.sv",
    RTL_DIR / "core" / "axis_if.sv",
    RTL_DIR / "eth"  / "eth_stack_wrapper.sv",
] + VERILOG_ETH_RTL + AXIS_LIB_RTL

def run(testcase=None):
    build_dir = TB_DIR / "sim_build"
    build_dir.mkdir(exist_ok=True)
    vvp_out = build_dir / "sim.vvp"

    # ---- Step 1: Compile ------------------------------------
    compile_cmd = [
        "iverilog",
        "-g2012",
        "-D", "ICARUS_SIM",
        "-I", str(RTL_DIR / "core"),
        "-o", str(vvp_out),
        "-s", "eth_stack_wrapper",
    ] + [str(s) for s in SOURCES]

    print("\n=== Compiling ===")
    result = subprocess.run(compile_cmd)
    if result.returncode != 0:
        print("COMPILE FAILED")
        sys.exit(1)

    # ---- Find base Python (has the DLL, unlike the venv) ----
    libpython_path = subprocess.check_output(
        ["cocotb-config", "--libpython"], text=True
    ).strip()
    base_python_home = str(Path(libpython_path).parent)

    print(f"\nLibpython:       {libpython_path}")
    print(f"Base Python home: {base_python_home}")

    # ---- Step 2: Run ----------------------------------------
    env = os.environ.copy()
    env["PATH"] = base_python_home + os.pathsep + env.get("PATH", "")
    env.update({
        "COCOTB_TEST_MODULES": "test_eth_stack",
        "TESTCASE":            testcase or "",
        "TOPLEVEL":            "eth_stack_wrapper",
        "TOPLEVEL_LANG":       "verilog",
        "COCOTB_RESOLVE_X":    "ZEROS",
        "PYTHONPATH":          str(TB_DIR),
        "COCOTB_LOG_LEVEL":    "INFO",
        "COCOTB_RESULTS_FILE": str(build_dir / "results.xml"),
        "PYTHONHOME":          base_python_home,
        "PYGPI_PYTHON_BIN":    str(Path(sys.executable)),
    })

    run_cmd = [
        "vvp",
        "-M", str(VENV / "Lib/site-packages/cocotb/libs"),
        "-m", "cocotbvpi_icarus",
        str(vvp_out),
    ]

    print(f"\nPYTHONPATH:      {env['PYTHONPATH']}")
    print(f"PYTHONHOME:      {env['PYTHONHOME']}")
    print(f"PYTHON_BIN:      {env['PYGPI_PYTHON_BIN']}")
    print(f"Test exists:     {(TB_DIR / 'test_eth_stack.py').exists()}")
    print(f"TB_DIR:          {TB_DIR}")

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