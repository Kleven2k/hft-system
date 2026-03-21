#!/usr/bin/env python3
"""
download.py - Download NASDAQ TotalView-ITCH 5.0 historical data files.

NASDAQ publishes free historical ITCH data at:
  https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/

Files are named: YYYYMMDD.NASDAQ_ITCH50.gz
A single day is ~5-15 GB compressed. The parser reads them as a stream
so you do NOT need to decompress them to disk.

Usage:
  # List available dates (scrapes the index page)
  python research/nasdaq/download.py --list

  # Download a specific date
  python research/nasdaq/download.py --date 20190130 --out research/data/nasdaq/

  # Download most recent available file
  python research/nasdaq/download.py --latest --out research/data/nasdaq/

Disk space warning:
  Each compressed file is 5-15 GB. Make sure your destination has space.
  The parser reads .gz files directly - no decompression needed.
"""

import argparse
import re
import sys
import urllib.request
from pathlib import Path

BASE_URL  = "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/"
INDEX_URL = BASE_URL


def list_available() -> list[str]:
    """Scrape the NASDAQ ITCH index page and return available file names."""
    print(f"Fetching index from {INDEX_URL} ...")
    try:
        with urllib.request.urlopen(INDEX_URL, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"Error fetching index: {e}")
        return []

    # Extract links matching the ITCH file pattern
    pattern = r'(\d{8}\.NASDAQ_ITCH50\.gz)'
    files = sorted(set(re.findall(pattern, html)))
    return files


def download(filename: str, out_dir: Path) -> Path:
    """Download a single ITCH file with a progress bar."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / filename

    if dest.exists():
        print(f"Already exists: {dest}  ({dest.stat().st_size / 1e9:.1f} GB)")
        return dest

    url = BASE_URL + filename
    print(f"Downloading {filename}")
    print(f"  URL: {url}")
    print(f"  Dest: {dest}")
    print(f"  Warning: file is typically 5-15 GB. This will take a while.")

    def progress(block_count, block_size, total):
        downloaded = block_count * block_size
        if total > 0:
            pct = downloaded / total * 100
            gb  = downloaded / 1e9
            tot = total / 1e9
            bar = "#" * int(pct / 2)
            print(f"\r  {pct:5.1f}%  {gb:.2f}/{tot:.2f} GB  [{bar:<50}]",
                  end="", flush=True)
        else:
            print(f"\r  {downloaded/1e9:.2f} GB downloaded", end="", flush=True)

    try:
        urllib.request.urlretrieve(url, dest, reporthook=progress)
        print(f"\n  Done. {dest.stat().st_size / 1e9:.2f} GB")
    except Exception as e:
        print(f"\n  Download failed: {e}")
        if dest.exists():
            dest.unlink()
        sys.exit(1)

    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Download NASDAQ ITCH 5.0 data")
    parser.add_argument("--list",   action="store_true", help="List available dates")
    parser.add_argument("--date",   default="",          help="Date YYYYMMDD to download")
    parser.add_argument("--latest", action="store_true", help="Download most recent file")
    parser.add_argument("--out",    default="research/data/nasdaq/",
                        help="Output directory (default: research/data/nasdaq/)")
    args = parser.parse_args()

    out_dir = Path(args.out)

    if args.list or args.latest or not args.date:
        files = list_available()
        if not files:
            print("No files found. Check your internet connection.")
            return

        print(f"\nAvailable ITCH files ({len(files)} dates):")
        for f in files[-20:]:   # show last 20
            local = out_dir / f
            status = f"  [downloaded {local.stat().st_size/1e9:.1f}GB]" if local.exists() else ""
            print(f"  {f}{status}")

        if args.latest:
            filename = files[-1]
            print(f"\nDownloading latest: {filename}")
            dest = download(filename, out_dir)
            print(f"\nRun backtest with:")
            print(f"  python research/backtest/nasdaq_mm_backtest.py --file {dest} --symbol AAPL --sweep")
        return

    if args.date:
        filename = f"{args.date}.NASDAQ_ITCH50.gz"
        dest = download(filename, out_dir)
        print(f"\nRun backtest with:")
        print(f"  python research/backtest/nasdaq_mm_backtest.py --file {dest} --symbol AAPL --sweep")


if __name__ == "__main__":
    main()
