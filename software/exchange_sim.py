#!/usr/bin/env python3
"""
exchange_sim.py — SoupBinTCP TCP server for FPGA hardware validation (Phase 27).

Acts as the exchange: accepts a TCP connection from the FPGA, handles the
SoupBinTCP session layer, and echoes execution reports back for every order.

Runs on the PC connected directly to the Nexys Video via Ethernet cable.
The FPGA's DST_IP/DST_PORT parameters must match this machine's IP and the
port configured here.

Usage:
    python software/exchange_sim.py [--port 4200] [--fill-delay 10]

Setup:
    1. Set PC Ethernet adapter to a static IP, e.g. 192.168.1.1 / 255.255.255.0
    2. Ensure tcp_engine.sv DST_IP  = 32'hC0A80101  (192.168.1.1)
               tcp_engine.sv SRC_IP  = 32'hC0A8010A  (192.168.1.10)
               tcp_engine.sv DST_PORT = 16'd4200
    3. Run this script on the PC
    4. Run feed_bridge.py to provide market data to the FPGA
    5. Run monitor.py to observe telemetry

SoupBinTCP framing (big-endian):
    [2] length  — bytes that follow, including the type byte
    [1] type
    [N] payload

Client → Server:
    'L' 0x4C  Login Request  (46-byte payload: user(6)+pass(10)+session(10)+seq(20 ASCII))
    'U' 0x55  Unsequenced Data  (OUCH order wrapped here)
    'R' 0x52  Client Heartbeat  (no payload)
    'O' 0x4F  Logout Request    (no payload)

Server → Client:
    'A' 0x41  Login Accepted  (30 bytes: session(10)+seq(20 ASCII))
    'J' 0x4A  Login Rejected  (1 byte: reason code)
    'S' 0x53  Sequenced Data  (OUCH execution report wrapped here)
    'H' 0x48  Server Heartbeat (no payload)
    'Z' 0x5A  End of Session  (no payload)

OUCH execution report types (inside 'S' frames):
    'A' 0x41  Order Accepted  — order is live, no fill yet
    'E' 0x45  Order Executed  — full or partial fill
    'C' 0x43  Order Cancelled
    'J' 0x4A  Order Rejected  (one reason byte follows)

Soup_session.sv FPGA response to execution reports:
    'A' → ACK_PARTIAL  (order live)
    'E' → ACK_FILLED + exec_shares
    'C' → ACK_CANCELLED
    'J' → ACK_REJECTED
"""

import argparse
import asyncio
import logging
import socket
import struct
import time

# ---------------------------------------------------------------------------
# Defaults — must match tcp_engine.sv parameters
# ---------------------------------------------------------------------------

LISTEN_HOST  = "0.0.0.0"
LISTEN_PORT  = 4200          # DST_PORT in tcp_engine.sv
HEARTBEAT_S  = 1.0           # send Server Heartbeat if idle this long
FILL_DELAY_S = 0.010         # seconds between Accepted and Executed reports

# OUCH symbol names for display (index matches ouch_encoder.sv STOCK_0..3)
SYMBOLS = ["AAPL  ", "MSFT  ", "AMZN  ", "TSLA  "]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("exchange_sim")


# ---------------------------------------------------------------------------
# SoupBinTCP framing helpers
# ---------------------------------------------------------------------------

def soup_frame(pkt_type: int, payload: bytes) -> bytes:
    """Build a SoupBinTCP frame: [2-byte big-endian length][type][payload]."""
    length = 1 + len(payload)
    return struct.pack(">H", length) + bytes([pkt_type]) + payload


def soup_login_accepted(session: str = "SIM       ", seq: int = 1) -> bytes:
    """Login Accepted ('A'): session(10) + seq_num(20 ASCII) = 30 payload bytes."""
    sess_b = session.ljust(10)[:10].encode()
    seq_b  = str(seq).rjust(20).encode()
    return soup_frame(0x41, sess_b + seq_b)


def soup_login_rejected(reason: int = ord('S')) -> bytes:
    """Login Rejected ('J'): 1-byte reason code. 'S'=not authorised."""
    return soup_frame(0x4A, bytes([reason]))


def soup_server_heartbeat() -> bytes:
    """Server Heartbeat ('H'): no payload."""
    return soup_frame(0x48, b"")


def soup_end_of_session() -> bytes:
    """End of Session ('Z'): no payload."""
    return soup_frame(0x5A, b"")


def soup_sequenced(ouch_payload: bytes) -> bytes:
    """Wrap an OUCH inbound message in a Sequenced Data ('S') frame."""
    return soup_frame(0x53, ouch_payload)


# ---------------------------------------------------------------------------
# OUCH inbound execution report builders
# ---------------------------------------------------------------------------

def exec_accepted(token: bytes) -> bytes:
    """
    OUCH Order Accepted (15 bytes):
        [0]    'A' (0x41)
        [1-14] order_token (14 bytes, echoed back)

    FPGA soup_session fires ACK_PARTIAL on receipt.
    Only 15 bytes needed; FPGA ends parse when rx_payload_cnt == frame_len-2.
    """
    return b'\x41' + token[:14]


def exec_executed(token: bytes, shares: int) -> bytes:
    """
    OUCH Order Executed (19 bytes):
        [0]    'E' (0x45)
        [1-14] order_token (14 bytes)
        [15-18] executed_shares (uint32 big-endian)

    FPGA soup_session fires ACK_FILLED + exec_shares on receipt.
    """
    return b'\x45' + token[:14] + struct.pack(">I", shares)


def exec_rejected(token: bytes, reason: int = ord('T')) -> bytes:
    """
    OUCH Order Rejected (16 bytes):
        [0]    'J' (0x4A)
        [1-14] order_token (14 bytes)
        [15]   reject_reason byte  ('T'=not entitled, 'Z'=no clearing acct, etc.)

    FPGA soup_session fires ACK_REJECTED on receipt.
    """
    return b'\x4A' + token[:14] + bytes([reason])


# ---------------------------------------------------------------------------
# SoupBinTCP receive buffer — reassembles TCP byte stream into frames
# ---------------------------------------------------------------------------

class SoupBuffer:
    """Accumulates raw TCP bytes and yields complete SoupBinTCP frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Feed raw bytes; return list of (pkt_type, payload) tuples."""
        self._buf += data
        frames: list[tuple[int, bytes]] = []
        while len(self._buf) >= 3:
            length = struct.unpack_from(">H", self._buf, 0)[0]
            if len(self._buf) < 2 + length:
                break
            pkt_type = self._buf[2]
            payload  = bytes(self._buf[3 : 2 + length])
            self._buf = self._buf[2 + length:]
            frames.append((pkt_type, payload))
        return frames


# ---------------------------------------------------------------------------
# OUCH order parser (bare OUCH bytes, no SoupBinTCP header)
# ---------------------------------------------------------------------------

def parse_ouch(payload: bytes) -> dict | None:
    """Parse a bare OUCH 4.2 message from a 'U' Unsequenced Data frame."""
    if not payload:
        return None
    msg_type = payload[0]

    if msg_type == 0x4F and len(payload) >= 30:   # Enter Order
        token     = payload[1:15]
        side      = chr(payload[15])
        shares    = struct.unpack_from(">I", payload, 16)[0]
        stock     = payload[20:26].decode(errors="replace").rstrip()
        price_raw = struct.unpack_from(">I", payload, 26)[0]
        price_f   = price_raw / 10000.0
        return {
            "type":   "ENTER",
            "token":  token,
            "side":   side,
            "shares": shares,
            "stock":  stock,
            "price":  price_f,
            "price_raw": price_raw,
        }

    elif msg_type == 0x58 and len(payload) >= 15:  # Cancel Order
        token = payload[1:15]
        return {"type": "CANCEL", "token": token}

    else:
        return None


def fmt_token(token: bytes) -> str:
    return token.decode(errors="replace")


# ---------------------------------------------------------------------------
# Per-connection session handler
# ---------------------------------------------------------------------------

async def handle_client(reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter,
                        fill_delay: float) -> None:
    peer = writer.get_extra_info("peername")
    log.info(f"TCP connection from {peer}")

    buf      = SoupBuffer()
    logged_in = False
    order_count = 0
    last_tx  = time.monotonic()

    async def send(frame: bytes) -> None:
        nonlocal last_tx
        writer.write(frame)
        await writer.drain()
        last_tx = time.monotonic()

    async def fill_order(token: bytes, shares: int) -> None:
        """Send Accepted immediately, then Executed after fill_delay."""
        await send(soup_sequenced(exec_accepted(token)))
        log.info(f"  → Accepted  token={fmt_token(token)}")
        await asyncio.sleep(fill_delay)
        await send(soup_sequenced(exec_executed(token, shares)))
        log.info(f"  → Executed  token={fmt_token(token)}  shares={shares}")

    try:
        while True:
            # ---- Heartbeat timer ----
            now = time.monotonic()
            timeout = max(0.05, HEARTBEAT_S - (now - last_tx))

            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            except asyncio.TimeoutError:
                if logged_in:
                    await send(soup_server_heartbeat())
                    log.debug("→ Server Heartbeat")
                continue

            if not data:
                log.info("Connection closed by FPGA")
                break

            for pkt_type, payload in buf.feed(data):
                # ---- Login Request ----
                if pkt_type == 0x4C:
                    if len(payload) >= 46:
                        user    = payload[0:6].decode(errors="replace").strip()
                        passwd  = payload[6:16].decode(errors="replace").strip()
                        session = payload[16:26].decode(errors="replace").strip()
                        seq_str = payload[26:46].decode(errors="replace").strip()
                        log.info(f"Login Request  user={user!r}  pass={passwd!r}"
                                 f"  session={session!r}  seq={seq_str}")
                    else:
                        log.warning(f"Short Login Request ({len(payload)} bytes)")

                    await send(soup_login_accepted())
                    logged_in = True
                    log.info("→ Login Accepted")

                # ---- Client Heartbeat ----
                elif pkt_type == 0x52:
                    log.debug("← Client Heartbeat")

                # ---- Logout Request ----
                elif pkt_type == 0x4F:
                    log.info("← Logout Request  → End of Session")
                    await send(soup_end_of_session())
                    return

                # ---- Unsequenced Data (OUCH order) ----
                elif pkt_type == 0x55:
                    order = parse_ouch(payload)
                    if order is None:
                        log.warning(f"Unrecognised OUCH  hex={payload.hex()}")
                        continue

                    if order["type"] == "ENTER":
                        order_count += 1
                        log.info(
                            f"Order #{order_count:4d}  "
                            f"{'BUY ' if order['side']=='B' else 'SELL'}  "
                            f"{order['stock']:6s}  "
                            f"qty={order['shares']:6d}  "
                            f"price=${order['price']:.4f}  "
                            f"token={fmt_token(order['token'])}"
                        )
                        asyncio.ensure_future(
                            fill_order(order["token"], order["shares"])
                        )

                    elif order["type"] == "CANCEL":
                        log.info(f"Cancel  token={fmt_token(order['token'])}")
                        # Send Cancelled report
                        await send(soup_sequenced(
                            b'\x43' + order["token"]  # 'C' + token
                        ))
                        log.info(f"  → Cancelled  token={fmt_token(order['token'])}")

                else:
                    log.debug(f"Unknown packet type 0x{pkt_type:02X}")

    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as e:
        log.info(f"Connection lost: {e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        log.info(f"Session ended  orders_received={order_count}")


# ---------------------------------------------------------------------------
# Startup: print network info to help user configure FPGA parameters
# ---------------------------------------------------------------------------

def print_network_info(port: int) -> None:
    log.info("=" * 60)
    log.info("exchange_sim — SoupBinTCP exchange simulator")
    log.info("=" * 60)
    log.info(f"Listening on port {port}")
    log.info("")
    log.info("PC IPv4 addresses (use one of these as FPGA DST_IP):")
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                log.info(f"  {ip}")
    except Exception:
        log.info("  (could not enumerate addresses)")
    log.info("")
    log.info("FPGA tcp_engine.sv parameters to match this machine:")
    log.info(f"  DST_PORT = 16'd{port}")
    log.info("  DST_IP   = 32'hXXXXXXXX  (from address above, in hex)")
    log.info("  Example: 192.168.1.1 → 32'hC0A80101")
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    print_network_info(args.port)

    fill_delay = args.fill_delay / 1000.0

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, fill_delay),
        LISTEN_HOST,
        args.port,
    )
    addrs = [s.getsockname() for s in server.sockets]
    log.info(f"Server ready on {addrs}")
    log.info("Waiting for FPGA connection (power on / reprogram board) ...")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SoupBinTCP exchange simulator for FPGA hardware validation"
    )
    parser.add_argument(
        "--port", type=int, default=LISTEN_PORT,
        help=f"TCP port to listen on (default {LISTEN_PORT}; must match DST_PORT in tcp_engine.sv)"
    )
    parser.add_argument(
        "--fill-delay", type=int, default=10, metavar="MS",
        help="Milliseconds between Order Accepted and Order Executed reports (default 10)"
    )
    args = parser.parse_args()
    asyncio.run(main(args))
