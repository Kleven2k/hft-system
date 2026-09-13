#!/usr/bin/env python3
"""
ack_simulator.py — Paper-trading ACK simulator for the FPGA OUCH engine

Listens on UDP PORT_OUCH (42000) for OUCH messages from the FPGA.
For every new order it sends back a FILLED ACK after FILL_LATENCY_MS.
For every cancel it sends back a CANCELLED ACK immediately.

FILL_LATENCY_MS simulates exchange round-trip time.  At 50 ms the order
rate drops to a realistic ~5-10 orders/sec per symbol instead of the
thousands/sec seen with zero latency.

OUCH new order (20 bytes, big-endian):
  [0]      msg_type  0x4F ('O')
  [1-2]   symbol_id  uint16
  [3]      side       0x42='B'  0x53='S'
  [4-7]   price       uint32
  [8-11]  quantity    uint32
  [12-19] order_id    uint64

OUCH cancel (9 bytes, big-endian):
  [0]     msg_type  0x58 ('X')
  [1-8]  order_id   uint64

ACK (13 bytes, big-endian) sent to FPGA PORT_OUCH_ACK:
  [0-7]  order_id  uint64
  [8]    status    0x00=FILLED  0x01=PARTIAL  0x02=REJECTED  0x03=CANCELLED
  [9-12] fill_qty  uint32

Usage:
  python ack_simulator.py [--latency 50]
"""

import argparse
import logging
import socket
import struct
import threading
import time

# ---------------------------------------------------------------------------
# Configuration — must match hft_pkg.sv
# ---------------------------------------------------------------------------

LISTEN_IP   = "0.0.0.0"
LISTEN_PORT = 42000       # PORT_OUCH — FPGA sends orders here

FPGA_IP     = "192.168.1.10"
ACK_PORT    = 42001       # PORT_OUCH_ACK — FPGA listens for ACKs here

# Simulated exchange round-trip latency in milliseconds.
# 50 ms ≈ typical crypto exchange.  Set to 0 for instant fills.
FILL_LATENCY_MS = 50

SIDE_NAMES  = {0x42: "BUY", 0x53: "SELL"}

# Log every Nth order to keep the terminal readable.  Set to 1 to log all.
LOG_EVERY_N = 10

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ack_sim")

# ---------------------------------------------------------------------------
# ACK sender (called from background thread after latency delay)
# ---------------------------------------------------------------------------

ACK_FILLED    = 0x00
ACK_CANCELLED = 0x03


def _send_ack(tx_sock: socket.socket, order_id: int, status: int,
              fill_qty: int, delay_s: float,
              cancelled_ids: set) -> None:
    if delay_s > 0:
        time.sleep(delay_s)
    # Only suppress FILLED — never suppress CANCELLED ACKs (FPGA needs them to
    # clear pending/canceling state, otherwise that slot is blocked forever).
    if status == ACK_FILLED and order_id in cancelled_ids:
        return   # cancel beat the fill — don't send phantom FILLED
    pkt = struct.pack(">QBI", order_id, status, fill_qty)
    tx_sock.sendto(pkt, (FPGA_IP, ACK_PORT))


def send_ack_async(tx_sock: socket.socket, order_id: int, status: int,
                   fill_qty: int, delay_s: float,
                   cancelled_ids: set) -> None:
    t = threading.Thread(target=_send_ack,
                         args=(tx_sock, order_id, status, fill_qty, delay_s,
                               cancelled_ids),
                         daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="FPGA OUCH ACK simulator")
    parser.add_argument("--latency", type=int, default=FILL_LATENCY_MS,
                        help=f"Fill latency in ms (default {FILL_LATENCY_MS})")
    args = parser.parse_args()
    delay_s = args.latency / 1000.0

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind((LISTEN_IP, LISTEN_PORT))
    rx.settimeout(1.0)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    log.info(f"Listening on UDP :{LISTEN_PORT}  (OUCH orders from FPGA)")
    log.info(f"Sending ACKs  to  {FPGA_IP}:{ACK_PORT}")
    log.info(f"Fill latency      {args.latency} ms")
    log.info(f"Logging every     {LOG_EVERY_N} orders  (set LOG_EVERY_N=1 for all)")
    log.info("Press Ctrl+C to stop")

    orders_seen  = 0
    cancels_seen = 0
    cancelled_ids: set = set()   # orders cancelled before fill fires

    try:
        while True:
            try:
                data, _ = rx.recvfrom(64)
            except socket.timeout:
                continue
            if not data:
                continue

            msg_type = data[0]

            if msg_type == 0x4F and len(data) >= 20:
                symbol_id = struct.unpack_from(">H", data, 1)[0]
                side      = data[3]
                price     = struct.unpack_from(">I", data, 4)[0]
                qty       = struct.unpack_from(">I", data, 8)[0]
                order_id  = struct.unpack_from(">Q", data, 12)[0]
                orders_seen += 1

                if orders_seen % LOG_EVERY_N == 0:
                    side_name = SIDE_NAMES.get(side, f"0x{side:02X}")
                    log.info(
                        f"ORDER #{orders_seen}  sym={symbol_id}  {side_name}"
                        f"  price={price}  qty={qty}  id={order_id:#018x}"
                        f"  (ack in {args.latency} ms)"
                    )

                send_ack_async(tx, order_id, ACK_FILLED, qty, delay_s, cancelled_ids)

            elif msg_type == 0x58 and len(data) >= 9:
                order_id = struct.unpack_from(">Q", data, 1)[0]
                cancels_seen += 1
                cancelled_ids.add(order_id)
                log.info(f"CANCEL #{cancels_seen}  id={order_id:#018x}")
                send_ack_async(tx, order_id, ACK_CANCELLED, 0, 0.0, cancelled_ids)

            else:
                log.warning(f"Unknown msg_type=0x{msg_type:02X}  len={len(data)}")

    except KeyboardInterrupt:
        pass
    finally:
        log.info(f"Stopped.  orders={orders_seen}  cancels={cancels_seen}")
        rx.close()
        tx.close()


if __name__ == "__main__":
    main()
