#!/usr/bin/env bash
# ============================================================
# build.sh — Two-stage Vivado build (memory-safe)
#
# Runs synth+place and route+bitstream as separate Vivado
# processes so peak memory never accumulates across stages.
# Each process exits cleanly before the next one starts.
#
# Usage: bash fpga/scripts/build.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FPGA_DIR="$(realpath "$SCRIPT_DIR/..")"
TS="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="$FPGA_DIR/runs/build_$TS"

mkdir -p "$RUN_DIR"

echo "============================================================"
echo " HFT-SYSTEM BUILD  →  $RUN_DIR"
echo "============================================================"

echo ""
echo "=== Stage 1: Synth + Place ==="
vivado -mode tcl \
       -source "$SCRIPT_DIR/build.tcl" \
       -tclargs "$RUN_DIR" \
       -nojournal -nolog

echo ""
echo "=== Stage 2: Route + Bitstream ==="
vivado -mode tcl \
       -source "$SCRIPT_DIR/route_from_checkpoint.tcl" \
       -tclargs "$RUN_DIR/post_place.dcp" \
       -nojournal -nolog

echo ""
echo "============================================================"
echo " BUILD COMPLETE: $RUN_DIR/hft_top.bit"
echo "============================================================"
