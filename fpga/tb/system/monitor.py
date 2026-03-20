#!/usr/bin/env python3
"""
monitor.py — Real-time FPGA telemetry display (Phase 20/21B)

Listens on UDP port 42002 for 64-byte telemetry packets from the FPGA
and displays a live dashboard updated once per second.

Usage:
    python fpga/tb/system/monitor.py

Packet format (big-endian, 64 bytes):
    [ 0- 3]  magic   0x48465401  ('HFT\\x01')
    [ 4- 7]  seq_num (uint32)
    [ 8-15]  order_id_cnt (uint64)  — total orders ever emitted
    [16-27]  slot 0: pos(4B) pnl(4B) reject(1B) token(1B) flags(1B) pad(1B)
    [28-39]  slot 1
    [40-51]  slot 2
    [52-63]  slot 3
      flags: bit0 = bid_valid, bit1 = ask_valid
      pnl:   signed int32, edge-vs-mid in price ticks (1 tick = $0.0001)
"""

import socket, struct, os, time, datetime

MY_IP      = "0.0.0.0"
TELEM_PORT = 42002
MAGIC      = b'\x48\x46\x54\x01'
PKT_BYTES  = 64
MAX_BURST  = 5  # must match strategy MAX_BURST parameter

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((MY_IP, TELEM_PORT))
sock.settimeout(5)

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def token_bar(tok, mx=MAX_BURST):
    filled = int(tok)
    return '[' + '█' * filled + '░' * (mx - filled) + ']'

def flags_str(f):
    return ('B' if f & 1 else '-') + ('A' if f & 2 else '-')

def pnl_str(pnl):
    dollars = pnl / 10000.0
    sign = '+' if dollars >= 0 else ''
    return f"{sign}{dollars:.4f}"

print(f"HFT Telemetry Monitor — listening on UDP {TELEM_PORT}")
print("Waiting for first packet (run test_itch.py on FPGA)...\n")

prev_seq    = None
prev_oid    = None
prev_time   = None
pkt_count   = 0

try:
    while True:
        try:
            data, addr = sock.recvfrom(256)
        except socket.timeout:
            print(f"\r[{datetime.datetime.now():%H:%M:%S}] No packet received (timeout 5 s) — FPGA running?", end='')
            continue

        if len(data) < PKT_BYTES or data[:4] != MAGIC:
            continue

        seq,      = struct.unpack('>I', data[4:8])
        oid_cnt,  = struct.unpack('>Q', data[8:16])

        slots = []
        for i in range(4):
            off = 16 + i * 12
            pos, = struct.unpack('>i', data[off:off+4])
            pnl, = struct.unpack('>i', data[off+4:off+8])
            reject  = data[off+8]
            token   = data[off+9]
            flags   = data[off+10]
            slots.append((pos, pnl, reject, token, flags))

        now = datetime.datetime.now()
        pkt_count += 1

        # Orders per second (delta over last packet)
        orders_ps = 0
        if prev_oid is not None and prev_time is not None:
            dt = (now - prev_time).total_seconds()
            if dt > 0:
                orders_ps = (oid_cnt - prev_oid) / dt

        dropped = (seq - prev_seq - 1) if prev_seq is not None and seq > prev_seq + 1 else 0

        clear()
        print(f"╔══════════════════════════════════════════════════════════════╗")
        print(f"║  HFT FPGA Telemetry              {now:%Y-%m-%d %H:%M:%S}    ║")
        print(f"╠══════════════════════════════════════════════════════════════╣")
        print(f"║  seq={seq:<8d}  total_orders={oid_cnt:<10d}  {orders_ps:5.1f} ord/s     ║")
        if dropped:
            print(f"║  *** {dropped} packet(s) dropped ***                               ║")
        print(f"╠══════════════════════════════════════════════════════════════╣")
        print(f"║  Slot  Position   P&L($)     Rejects  Tokens     BV AV       ║")
        print(f"╠══════════════════════════════════════════════════════════════╣")
        for i, (pos, pnl, rej, tok, flg) in enumerate(slots):
            bv  = '✓' if flg & 1 else '✗'
            av  = '✓' if flg & 2 else '✗'
            sl  = ' SL!' if flg & 4 else '    '
            bar = token_bar(tok)
            ps  = pnl_str(pnl)
            print(f"║   {i}   {pos:+8d}   {ps:>10s}     {rej:3d}    {bar}  {bv}  {av}{sl} ║")
        print(f"╚══════════════════════════════════════════════════════════════╝")
        print(f"  Packets received: {pkt_count}   Ctrl-C to exit")

        prev_seq  = seq
        prev_oid  = oid_cnt
        prev_time = now

except KeyboardInterrupt:
    print("\nMonitor stopped.")
finally:
    sock.close()
