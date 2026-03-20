# ============================================================
# build.tcl — Vivado synth + place (Stage 1 of 2)
#
# Called by build.sh with a pre-created RUN_DIR argument so
# both stages share the same output directory.
#
# Usage (via wrapper):  bash fpga/scripts/build.sh
# Usage (direct):       vivado -mode tcl -source fpga/scripts/build.tcl \
#                               -tclargs <run_dir>
# ============================================================

# ---- Configuration -----------------------------------------
set BOARD       "nexys_video"
set TOP         "hft_top"
set PART        "xc7a200tsbg484-1"

# ---- Paths -------------------------------------------------
set SCRIPT_DIR  [file dirname [file normalize [info script]]]
set REPO_ROOT   [file normalize "$SCRIPT_DIR/../.."]
set FPGA_DIR    "$REPO_ROOT/fpga"
set RTL_DIR     "$FPGA_DIR/rtl"
set XDC_DIR     "$FPGA_DIR/constraints"
set TP_DIR      "$FPGA_DIR/third_party/verilog-ethernet"
set VE          "$TP_DIR/rtl"
set AX          "$TP_DIR/lib/axis/rtl"

# ---- Timestamped output directory -------------------------
set TS      [clock format [clock seconds] -format "%Y%m%d_%H%M%S"]
set RUN_DIR "$FPGA_DIR/runs/build_$TS"
file mkdir $RUN_DIR

# Limit parallel threads to reduce peak memory
set_param general.maxThreads 1

puts "============================================================"
puts " HFT-SYSTEM BUILD"
puts " Part:    $PART"
puts " Top:     $TOP"
puts " Out dir: $RUN_DIR"
puts "============================================================"

# ---- Read RTL ----------------------------------------------
read_verilog -sv [glob $RTL_DIR/core/*.sv]
read_verilog -sv [glob $RTL_DIR/eth/*.sv]   ;# includes telemetry_tx.sv
read_verilog -sv [glob $RTL_DIR/market_data/*.sv]
read_verilog -sv [glob $RTL_DIR/order_book/*.sv]
read_verilog -sv [glob $RTL_DIR/strategy/*.sv]
read_verilog -sv [glob $RTL_DIR/order_entry/*.sv]
read_verilog -sv [glob $RTL_DIR/top/*.sv]

# ---- Read verilog-ethernet (explicit — only what we use) ---
# Glob is avoided because the rtl/ folder contains 10GbE/XGMII
# files that reference undefined macros and fail elaboration.
read_verilog [list \
    $VE/ssio_ddr_in.v \
    $VE/ssio_ddr_out.v \
    $VE/ssio_sdr_in.v \
    $VE/ssio_sdr_out.v \
    $VE/oddr.v \
    $VE/iddr.v \
    $VE/rgmii_phy_if.v \
    $VE/eth_mac_1g_rgmii_fifo.v \
    $VE/eth_mac_1g_rgmii.v \
    $VE/eth_mac_1g.v \
    $VE/mac_ctrl_rx.v \
    $VE/mac_ctrl_tx.v \
    $VE/mac_pause_ctrl_rx.v \
    $VE/mac_pause_ctrl_tx.v \
    $VE/axis_gmii_rx.v \
    $VE/axis_gmii_tx.v \
    $VE/axis_eth_fcs.v \
    $VE/axis_eth_fcs_check.v \
    $VE/axis_eth_fcs_insert.v \
    $VE/eth_axis_rx.v \
    $VE/eth_axis_tx.v \
    $VE/eth_arb_mux.v \
    $VE/eth_mux.v \
    $VE/eth_demux.v \
    $VE/udp_complete.v \
    $VE/udp.v \
    $VE/udp_checksum_gen.v \
    $VE/udp_ip_rx.v \
    $VE/udp_ip_tx.v \
    $VE/udp_arb_mux.v \
    $VE/udp_mux.v \
    $VE/udp_demux.v \
    $VE/ip_complete.v \
    $VE/ip.v \
    $VE/ip_eth_rx.v \
    $VE/ip_eth_tx.v \
    $VE/ip_arb_mux.v \
    $VE/ip_mux.v \
    $VE/ip_demux.v \
    $VE/arp.v \
    $VE/arp_cache.v \
    $VE/arp_eth_rx.v \
    $VE/arp_eth_tx.v \
    $VE/lfsr.v \
    $AX/arbiter.v \
    $AX/priority_encoder.v \
    $AX/axis_fifo.v \
    $AX/axis_async_fifo.v \
    $AX/axis_async_fifo_adapter.v \
]

# ---- Read constraints --------------------------------------
read_xdc $XDC_DIR/nexys_video.xdc

# ---- Synthesis ---------------------------------------------
puts "\n--- Synthesis ---"
synth_design \
    -top $TOP \
    -part $PART \
    -include_dirs "$RTL_DIR/core"

write_checkpoint -force "$RUN_DIR/post_synth.dcp"
report_utilization -file "$RUN_DIR/util_synth.rpt"
report_timing_summary -file "$RUN_DIR/timing_synth.rpt"

# ---- Implementation ----------------------------------------
puts "\n--- Optimize ---"
opt_design

puts "\n--- Place ---"
place_design
write_checkpoint -force "$RUN_DIR/post_place.dcp"
report_timing_summary -file "$RUN_DIR/timing_place.rpt"

puts "\n--- Route ---"
route_design

puts "\n--- Post-route physical optimization ---"
phys_opt_design -directive AggressiveExplore

puts "\n--- Saving checkpoint ---"
write_checkpoint -force "$RUN_DIR/post_route.dcp"

puts "\n--- Reports ---"
report_timing_summary -file "$RUN_DIR/timing.rpt"
report_utilization    -file "$RUN_DIR/util.rpt"
report_power          -file "$RUN_DIR/power.rpt"
report_drc            -file "$RUN_DIR/drc.rpt"

puts "\n--- Bitstream ---"
write_bitstream -force "$RUN_DIR/hft_top.bit"

puts "\n============================================================"
puts " BUILD COMPLETE: $RUN_DIR/hft_top.bit"
puts "============================================================"