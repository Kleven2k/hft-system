# ============================================================
# route_from_checkpoint.tcl — Resume routing from post_place.dcp
#
# Usage:
#   vivado -mode tcl -source fpga/scripts/route_from_checkpoint.tcl \
#          -tclargs <path_to_post_place.dcp>
#
# Skips synthesis and placement; runs route → phys_opt → bitstream.
# Use this when the full build crashes after placement but before
# writing the bitfile (e.g. out-of-memory in write_checkpoint).
# ============================================================

set PART "xc7a200tsbg484-1"

if { $argc < 1 } {
    # Default: pick the most recent post_place.dcp automatically
    set SCRIPT_DIR [file dirname [file normalize [info script]]]
    set RUNS_DIR   [file normalize "$SCRIPT_DIR/../runs"]
    set candidates [lsort -decreasing [glob -nocomplain "$RUNS_DIR/*/post_place.dcp"]]
    if { [llength $candidates] == 0 } {
        puts "ERROR: No post_place.dcp found under $RUNS_DIR"
        exit 1
    }
    set DCP [lindex $candidates 0]
} else {
    set DCP [lindex $argv 0]
}

set RUN_DIR [file dirname $DCP]

puts "============================================================"
puts " RESUME FROM CHECKPOINT"
puts " DCP:     $DCP"
puts " Out dir: $RUN_DIR"
puts "============================================================\n"

# Limit parallel threads to reduce peak memory
set_param general.maxThreads 2

# Open placed design
open_checkpoint $DCP

puts "\n--- Route ---"
route_design

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
puts " DONE: $RUN_DIR/hft_top.bit"
puts "============================================================"
