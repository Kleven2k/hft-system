#!/usr/bin/env bash
# Pulls collected tick data from the Raspberry Pi to the local research/data/ folder,
# then deletes the Pi-side copies of files from previous days (keeps today's file,
# since the collector still has it open for writing).
set -euo pipefail

# Set PI_HOST in your shell/.env, e.g. PI_HOST="user@100.x.x.x" (Tailscale IP)
: "${PI_HOST:?Set PI_HOST=user@host before running this script}"
PI_DATA_DIR="~/hft-system/research/data"
LOCAL_DATA_DIR="$(dirname "$0")/../data/"
TODAY="$(date +%Y%m%d)"

mkdir -p "$LOCAL_DATA_DIR"
scp "$PI_HOST:${PI_DATA_DIR}/*.csv" "$LOCAL_DATA_DIR"
echo "Synced to $LOCAL_DATA_DIR"

ssh "$PI_HOST" "find ${PI_DATA_DIR} -name '*.csv' ! -name '*${TODAY}*' -delete -print"
echo "Deleted Pi-side files older than today ($TODAY)"
