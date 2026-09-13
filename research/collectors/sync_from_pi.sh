#!/usr/bin/env bash
# Pulls collected tick data from the Raspberry Pi to the local research/data/ folder,
# then deletes the Pi-side copies of files from previous days (keeps today's file,
# since the collector still has it open for writing).
set -euo pipefail

PI_HOST="fredrikpi@100.70.245.92"
PI_DATA_DIR="~/hft-system/research/data"
LOCAL_DATA_DIR="$(dirname "$0")/../data/"
TODAY="$(date +%Y%m%d)"

mkdir -p "$LOCAL_DATA_DIR"
scp "$PI_HOST:${PI_DATA_DIR}/*.csv" "$LOCAL_DATA_DIR"
echo "Synced to $LOCAL_DATA_DIR"

ssh "$PI_HOST" "find ${PI_DATA_DIR} -name '*.csv' ! -name '*${TODAY}*' -delete -print"
echo "Deleted Pi-side files older than today ($TODAY)"
