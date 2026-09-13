#!/usr/bin/env python3
"""
ouch_session.py — SoupBinTCP / OUCH 4.2 session layer

Bridges the FPGA UDP order stream to a real exchange over TCP/SoupBinTCP.
Replaces ack_simulator.py in production.

Architecture:
  FPGA → UDP:42000  → [this process] → TCP/SoupBinTCP → Exchange
  FPGA ← UDP:42001  ←                ← execution reports

SoupBinTCP framing (all lengths big-endian):
  [2] length  — number of bytes that follow (includes type byte)
  [1] type
  [N] payload

Client → Server packet types:
  'L' 0x4C  Login Request    (46-byte payload)
  'U' 0x55  Unsequenced Data (OUCH order payload)
  'R' 0x52  Client Heartbeat (empty payload)
  'O' 0x4F  Logout Request   (empty payload)

Server → Client packet types:
  'A' 0x41  Login Accepted
  'J' 0x4A  Login Rejected
  'S' 0x53  Sequenced Data   (OUCH execution report)
  'H' 0x48  Server Heartbeat
  'Z' 0x5A  End of Session

OUCH 4.2 Enter Order (client → exchange, 49 bytes):
  [0]     msg_type    'O' (0x4F)
  [1-14]  order_token 14 bytes ASCII, right-space-padded
  [15]    buy_sell    'B' or 'S'
  [16-19] shares      uint32 big-endian
  [20-25] stock       6 bytes ASCII, right-space-padded
  [26-29] price       uint32 big-endian ($0.0001 per unit)
  [30-33] time_in_force uint32 (0=day, 99998=IOC, 99999=GTC)
  [34-39] firm        6 bytes ASCII, right-space-padded
  [40]    display     'Y'
  [41]    capacity    'A' (agency) / 'P' (principal)
  [42]    iso_eligible 'N'
  [43-46] min_qty     uint32 (0 = no minimum)
  [47]    cross_type  'N'
  [48]    customer_type ' ' (retail default)

OUCH 4.2 Cancel Order (client → exchange, 15 bytes):
  [0]     msg_type    'X' (0x58)
  [1-14]  order_token 14 bytes ASCII (must match original Enter Order token)

FPGA internal OUCH format (received on UDP 42000):
  New order (20 bytes):
    [0]     0x4F 'O'
    [1-2]   symbol_id  uint16
    [3]     side       0x42='B'  0x53='S'
    [4-7]   price      uint32
    [8-11]  quantity   uint32
    [12-19] order_id   uint64
  Cancel (9 bytes):
    [0]     0x58 'X'
    [1-8]   order_id  uint64

ACK format sent back to FPGA (UDP 42001, 13 bytes):
  [0-7]  order_id  uint64
  [8]    status    0x00=FILLED  0x01=PARTIAL  0x02=REJECTED  0x03=CANCELLED
  [9-12] fill_qty  uint32

Usage:
  python ouch_session.py --host 192.168.1.100 --port 4200 \\
                         --user MYUSER --password MYPASSWORD \\
                         --session "          " --symbols AAPL,MSFT,AMZN,TSLA

  For paper trading without exchange connectivity:
  python ouch_session.py --paper [--latency 50]
"""

import argparse
import asyncio
import logging
import socket
import struct
import time
from enum import Enum, auto

# ---------------------------------------------------------------------------
# Configuration defaults — must match hft_pkg.sv
# ---------------------------------------------------------------------------

FPGA_IP        = "192.168.1.10"
LISTEN_IP      = "0.0.0.0"
LISTEN_PORT    = 42000          # PORT_OUCH — FPGA sends orders here
ACK_PORT       = 42001          # PORT_OUCH_ACK — FPGA expects ACKs here

HEARTBEAT_S    = 1.0            # send client heartbeat if idle for this long
HEARTBEAT_TIMEOUT_S = 15.0      # disconnect if no server heartbeat for this long

# Default symbol map: FPGA symbol_id → exchange stock symbol (6-char, space-padded)
# Override with --symbols AAPL,MSFT,AMZN,TSLA
DEFAULT_SYMBOLS = ["AAPL  ", "MSFT  ", "AMZN  ", "TSLA  "]

FIRM            = "MYFIRM"      # 6-char MPID, override with --firm
CAPACITY        = "A"           # 'A' agency, 'P' principal
TIME_IN_FORCE   = 99998         # IOC — immediate-or-cancel

# ACK status codes (matches hft_pkg.sv)
ACK_FILLED    = 0x00
ACK_PARTIAL   = 0x01
ACK_REJECTED  = 0x02
ACK_CANCELLED = 0x03

# OUCH inbound execution report types
EXEC_ACCEPTED  = ord('A')
EXEC_REPLACED  = ord('U')
EXEC_CANCELLED = ord('C')
EXEC_EXECUTED  = ord('E')
EXEC_BROKEN    = ord('B')
EXEC_REJECTED  = ord('J')
EXEC_SYSTEM    = ord('S')

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S.%f",
)
log = logging.getLogger("ouch_session")


# ---------------------------------------------------------------------------
# SoupBinTCP framing helpers
# ---------------------------------------------------------------------------

def soup_frame(pkt_type: int, payload: bytes) -> bytes:
    """Wrap payload in a SoupBinTCP frame: [2-byte length][1-byte type][payload]."""
    length = 1 + len(payload)
    return struct.pack(">H", length) + bytes([pkt_type]) + payload


def soup_login(username: str, password: str, session: str, seq: int) -> bytes:
    """Build a SoupBinTCP Login Request ('L') frame."""
    user_b  = username.ljust(6)[:6].encode()
    pass_b  = password.ljust(10)[:10].encode()
    sess_b  = session.ljust(10)[:10].encode()
    seq_b   = str(seq).rjust(20).encode()   # 20-byte ASCII sequence number
    return soup_frame(ord('L'), user_b + pass_b + sess_b + seq_b)


def soup_heartbeat() -> bytes:
    """Build a SoupBinTCP Client Heartbeat ('R') frame."""
    return soup_frame(ord('R'), b"")


def soup_logout() -> bytes:
    """Build a SoupBinTCP Logout Request ('O') frame."""
    return soup_frame(ord('O'), b"")


def soup_unsequenced(payload: bytes) -> bytes:
    """Wrap an OUCH message in a SoupBinTCP Unsequenced Data ('U') frame."""
    return soup_frame(ord('U'), payload)


# ---------------------------------------------------------------------------
# OUCH 4.2 message builders
# ---------------------------------------------------------------------------

def _token(order_id: int) -> bytes:
    """Convert 64-bit order_id to a 14-byte ASCII order token (right-space-padded)."""
    s = f"{order_id:014d}"[:14]
    return s.encode().ljust(14)[:14]


def ouch_enter_order(order_id: int, side: int, shares: int,
                     stock: str, price: int, firm: str,
                     tif: int, capacity: str) -> bytes:
    """Build a 49-byte OUCH 4.2 Enter Order message."""
    token    = _token(order_id)
    stock_b  = stock.ljust(6)[:6].encode()
    firm_b   = firm.ljust(6)[:6].encode()
    side_b   = b'B' if side == 0x42 else b'S'
    return (
        b'\x4F'             # 'O'
        + token             # [1-14]  order token
        + side_b            # [15]    buy/sell
        + struct.pack(">I", shares)   # [16-19] shares
        + stock_b           # [20-25] stock
        + struct.pack(">I", price)    # [26-29] price
        + struct.pack(">I", tif)      # [30-33] time in force
        + firm_b            # [34-39] firm
        + b'Y'              # [40]    display
        + capacity.encode() # [41]    capacity
        + b'N'              # [42]    ISO eligible
        + struct.pack(">I", 0)        # [43-46] min qty
        + b'N'              # [47]    cross type
        + b' '              # [48]    customer type
    )


def ouch_cancel_order(order_id: int) -> bytes:
    """Build a 15-byte OUCH 4.2 Cancel Order message."""
    return b'\x58' + _token(order_id)


# ---------------------------------------------------------------------------
# Parse inbound OUCH execution reports (exchange → client)
# ---------------------------------------------------------------------------

def parse_execution_report(data: bytes) -> dict | None:
    """
    Parse an OUCH 4.2 inbound message from SoupBinTCP Sequenced Data payload.
    Returns a dict with at least 'type', 'order_id', 'status', 'fill_qty'.
    Returns None if unrecognised.
    """
    if not data:
        return None
    msg_type = data[0]

    if msg_type == EXEC_ACCEPTED and len(data) >= 66:
        # Accepted: token[1-14] ... order_ref_num[30-37] ...
        token = data[1:15].decode(errors="replace").strip()
        order_id = int(token) if token.isdigit() else 0
        return {"type": "ACCEPTED", "order_id": order_id,
                "status": ACK_PARTIAL, "fill_qty": 0}

    elif msg_type == EXEC_EXECUTED and len(data) >= 40:
        # Executed: token[1-14], executed_shares[15-18], exec_price[19-22], ...
        token    = data[1:15].decode(errors="replace").strip()
        order_id = int(token) if token.isdigit() else 0
        fill_qty = struct.unpack_from(">I", data, 15)[0]
        return {"type": "EXECUTED", "order_id": order_id,
                "status": ACK_FILLED, "fill_qty": fill_qty}

    elif msg_type == EXEC_CANCELLED and len(data) >= 28:
        token    = data[1:15].decode(errors="replace").strip()
        order_id = int(token) if token.isdigit() else 0
        return {"type": "CANCELLED", "order_id": order_id,
                "status": ACK_CANCELLED, "fill_qty": 0}

    elif msg_type == EXEC_REJECTED and len(data) >= 27:
        token    = data[1:15].decode(errors="replace").strip()
        order_id = int(token) if token.isdigit() else 0
        reason   = chr(data[26]) if len(data) > 26 else '?'
        log.warning(f"Order REJECTED  token={token}  reason={reason!r}")
        return {"type": "REJECTED", "order_id": order_id,
                "status": ACK_REJECTED, "fill_qty": 0}

    elif msg_type == EXEC_SYSTEM:
        return None   # System event — ignore silently

    else:
        log.debug(f"Unhandled inbound msg_type=0x{msg_type:02X} len={len(data)}")
        return None


# ---------------------------------------------------------------------------
# SoupBinTCP receive buffer — reassembles TCP stream into frames
# ---------------------------------------------------------------------------

class SoupBuffer:
    """Reassembles a TCP byte stream into SoupBinTCP frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Feed raw TCP bytes; return list of (pkt_type, payload) tuples."""
        self._buf += data
        frames: list[tuple[int, bytes]] = []
        while len(self._buf) >= 3:
            length = struct.unpack_from(">H", self._buf, 0)[0]
            if len(self._buf) < 2 + length:
                break
            pkt_type = self._buf[2]
            payload  = bytes(self._buf[3 : 2 + length])
            self._buf = self._buf[2 + length :]
            frames.append((pkt_type, payload))
        return frames


# ---------------------------------------------------------------------------
# Session state machine
# ---------------------------------------------------------------------------

class SessionState(Enum):
    DISCONNECTED = auto()
    LOGGING_IN   = auto()
    ACTIVE       = auto()
    LOGGING_OUT  = auto()


# ---------------------------------------------------------------------------
# Paper-trading mode (no TCP, just simulates fills like ack_simulator.py)
# ---------------------------------------------------------------------------

class PaperSession:
    """Replaces the real session in --paper mode."""

    def __init__(self, latency_ms: int, ack_sock: socket.socket) -> None:
        self._latency = latency_ms / 1000.0
        self._sock    = ack_sock

    def submit_order(self, order_id: int, side: int, shares: int,
                     stock: str, price: int) -> None:
        log.info(f"PAPER ORDER  stock={stock.strip()}  "
                 f"{'BUY' if side == 0x42 else 'SELL'}  "
                 f"shares={shares}  price={price}  id={order_id:#018x}")
        asyncio.get_event_loop().call_later(
            self._latency, self._send_filled, order_id, shares)

    def cancel_order(self, order_id: int) -> None:
        log.info(f"PAPER CANCEL  id={order_id:#018x}")
        self._send_ack(order_id, ACK_CANCELLED, 0)

    def _send_filled(self, order_id: int, qty: int) -> None:
        self._send_ack(order_id, ACK_FILLED, qty)

    def _send_ack(self, order_id: int, status: int, fill_qty: int) -> None:
        pkt = struct.pack(">QBI", order_id, status, fill_qty)
        self._sock.sendto(pkt, (FPGA_IP, ACK_PORT))


# ---------------------------------------------------------------------------
# Main async engine
# ---------------------------------------------------------------------------

async def run_session(args: argparse.Namespace) -> None:
    """Main coroutine — manages TCP session and UDP bridge."""

    # ---- UDP socket (receives FPGA orders, sends ACKs) ----
    loop = asyncio.get_running_loop()
    ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ack_sock.setblocking(False)

    udp_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_rx.bind((LISTEN_IP, LISTEN_PORT))
    udp_rx.setblocking(False)

    # ---- Symbol map ----
    symbol_map: dict[int, str] = {}
    for i, sym in enumerate(args.symbols):
        symbol_map[i] = sym.ljust(6)[:6]

    log.info(f"Symbol map: {symbol_map}")

    # ---- Paper mode shortcut ----
    if args.paper:
        log.info("Paper-trading mode (no exchange connection)")
        paper = PaperSession(args.latency, ack_sock)
        while True:
            try:
                data = await loop.sock_recv(udp_rx, 64)
                _dispatch_fpga_pkt(data, symbol_map, args, None, paper, ack_sock)
            except Exception as e:
                log.error(f"UDP error: {e}")
        return

    # ---- Live mode: SoupBinTCP TCP session ----
    state   = SessionState.DISCONNECTED
    in_seq  = 1      # next expected inbound sequence number
    soup_buf = SoupBuffer()
    last_tx_time = 0.0
    last_rx_time = 0.0
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None

    async def tcp_connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        log.info(f"Connecting to {args.host}:{args.port} ...")
        r, w = await asyncio.open_connection(args.host, args.port)
        log.info("TCP connected")
        return r, w

    async def send_tcp(frame: bytes) -> None:
        nonlocal last_tx_time
        if writer:
            writer.write(frame)
            await writer.drain()
            last_tx_time = time.monotonic()

    def send_ack(order_id: int, status: int, fill_qty: int) -> None:
        pkt = struct.pack(">QBI", order_id, status, fill_qty)
        ack_sock.sendto(pkt, (FPGA_IP, ACK_PORT))

    while True:
        # ---- (Re)connect ----
        try:
            reader, writer = await tcp_connect()
        except Exception as e:
            log.error(f"Connect failed: {e} — retry in 5s")
            await asyncio.sleep(5)
            continue

        state = SessionState.LOGGING_IN
        last_rx_time = time.monotonic()

        # Send Login
        login_frame = soup_login(args.user, args.password, args.session, in_seq)
        await send_tcp(login_frame)
        log.info(f"Sent Login  user={args.user}  session={args.session!r}  seq={in_seq}")

        # ---- Event loop: TCP reads + UDP reads + heartbeat ----
        try:
            while True:
                now = time.monotonic()

                # Server heartbeat timeout
                if state == SessionState.ACTIVE:
                    if now - last_rx_time > HEARTBEAT_TIMEOUT_S:
                        log.error("Server heartbeat timeout — reconnecting")
                        break
                    if now - last_tx_time > HEARTBEAT_S:
                        await send_tcp(soup_heartbeat())
                        log.debug("Sent client heartbeat")

                # Read TCP (non-blocking check)
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=0.01)
                    if not chunk:
                        log.warning("Server closed connection")
                        break
                    last_rx_time = time.monotonic()
                    for pkt_type, payload in soup_buf.feed(chunk):
                        _handle_server_pkt(pkt_type, payload, state,
                                           send_ack, args)
                        # Update state from server responses
                        if pkt_type == ord('A'):
                            log.info(f"Login Accepted  session={payload[:10].decode(errors='replace').strip()!r}  "
                                     f"seq={payload[10:30].decode(errors='replace').strip()}")
                            state = SessionState.ACTIVE
                        elif pkt_type == ord('J'):
                            reason = payload[0:1].decode(errors="replace")
                            log.error(f"Login Rejected  reason={reason!r}")
                            state = SessionState.DISCONNECTED
                            break
                        elif pkt_type == ord('Z'):
                            log.warning("End of Session from server")
                            state = SessionState.DISCONNECTED
                            break
                        elif pkt_type == ord('S'):
                            in_seq += 1
                except asyncio.TimeoutError:
                    pass

                if state == SessionState.DISCONNECTED:
                    break

                # Read UDP from FPGA (non-blocking)
                try:
                    data = await loop.sock_recv(udp_rx, 64)
                    if state == SessionState.ACTIVE:
                        await _dispatch_fpga_pkt_live(
                            data, symbol_map, args, send_tcp, send_ack)
                except BlockingIOError:
                    pass
                except Exception as e:
                    log.error(f"UDP recv error: {e}")

        except Exception as e:
            log.error(f"Session error: {e}")
        finally:
            if writer:
                try:
                    await send_tcp(soup_logout())
                except Exception:
                    pass
                writer.close()
            log.info("Disconnected — reconnect in 5s")
            await asyncio.sleep(5)
            soup_buf = SoupBuffer()


def _handle_server_pkt(pkt_type: int, payload: bytes, state: SessionState,
                        send_ack, args) -> None:
    """Handle a decoded SoupBinTCP server packet."""
    if pkt_type == ord('H'):
        log.debug("Server heartbeat")
    elif pkt_type == ord('S'):
        # Sequenced Data: an OUCH inbound execution report
        report = parse_execution_report(payload)
        if report:
            log.info(f"EXEC {report['type']}  id={report['order_id']}  "
                     f"status={report['status']}  fill_qty={report['fill_qty']}")
            send_ack(report["order_id"], report["status"], report["fill_qty"])
    elif pkt_type not in (ord('A'), ord('J'), ord('Z')):
        log.debug(f"Server pkt type=0x{pkt_type:02X} len={len(payload)}")


def _dispatch_fpga_pkt(data: bytes, symbol_map: dict, args,
                        send_tcp, paper: "PaperSession | None",
                        ack_sock: socket.socket) -> None:
    """Synchronous dispatch for paper mode."""
    if not data:
        return
    msg_type = data[0]
    if msg_type == 0x4F and len(data) >= 20:   # New order
        symbol_id = struct.unpack_from(">H", data, 1)[0]
        side      = data[3]
        price     = struct.unpack_from(">I", data, 4)[0]
        qty       = struct.unpack_from(">I", data, 8)[0]
        order_id  = struct.unpack_from(">Q", data, 12)[0]
        stock     = symbol_map.get(symbol_id, f"SYM{symbol_id} ")
        if paper:
            paper.submit_order(order_id, side, qty, stock, price)
    elif msg_type == 0x58 and len(data) >= 9:   # Cancel
        order_id = struct.unpack_from(">Q", data, 1)[0]
        if paper:
            paper.cancel_order(order_id)
    else:
        log.warning(f"Unknown FPGA msg 0x{msg_type:02X} len={len(data)}")


async def _dispatch_fpga_pkt_live(data: bytes, symbol_map: dict, args,
                                   send_tcp, send_ack) -> None:
    """Async dispatch for live mode — translates FPGA format → OUCH 4.2 → TCP."""
    if not data:
        return
    msg_type = data[0]

    if msg_type == 0x4F and len(data) >= 20:   # New order
        symbol_id = struct.unpack_from(">H", data, 1)[0]
        side      = data[3]
        price     = struct.unpack_from(">I", data, 4)[0]
        qty       = struct.unpack_from(">I", data, 8)[0]
        order_id  = struct.unpack_from(">Q", data, 12)[0]
        stock     = symbol_map.get(symbol_id, f"SYM{symbol_id} ")

        ouch_msg  = ouch_enter_order(order_id, side, qty, stock, price,
                                      args.firm, TIME_IN_FORCE, CAPACITY)
        frame     = soup_unsequenced(ouch_msg)
        await send_tcp(frame)
        log.info(f"ORDER → exchange  stock={stock.strip()}  "
                 f"{'BUY' if side == 0x42 else 'SELL'}  "
                 f"qty={qty}  price={price}  id={order_id:#018x}")

    elif msg_type == 0x58 and len(data) >= 9:  # Cancel
        order_id  = struct.unpack_from(">Q", data, 1)[0]
        ouch_msg  = ouch_cancel_order(order_id)
        frame     = soup_unsequenced(ouch_msg)
        await send_tcp(frame)
        log.info(f"CANCEL → exchange  id={order_id:#018x}")

    else:
        log.warning(f"Unknown FPGA msg 0x{msg_type:02X} len={len(data)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="SoupBinTCP/OUCH 4.2 session layer")

    # Exchange connectivity
    p.add_argument("--host",     default="",        help="Exchange host (required for live)")
    p.add_argument("--port",     type=int, default=4200, help="Exchange TCP port")
    p.add_argument("--user",     default="USER  ",  help="SoupBinTCP username (6 chars)")
    p.add_argument("--password", default="PASSWORD  ", help="SoupBinTCP password (10 chars)")
    p.add_argument("--session",  default="          ", help="Requested session (10 chars, blank=any)")
    p.add_argument("--firm",     default=FIRM,       help="MPID / firm code (6 chars)")

    # Symbol mapping: --symbols AAPL,MSFT,AMZN,TSLA (index = FPGA symbol_id)
    p.add_argument("--symbols",  default=",".join(s.strip() for s in DEFAULT_SYMBOLS),
                   help="Comma-separated stock symbols, index = FPGA symbol_id")

    # Paper trading mode
    p.add_argument("--paper",   action="store_true", help="Paper-trading mode (no TCP)")
    p.add_argument("--latency", type=int, default=50, help="Paper fill latency ms")

    args = p.parse_args()
    args.symbols = [s.ljust(6)[:6] for s in args.symbols.split(",")]

    if not args.paper and not args.host:
        p.error("--host is required for live mode (or use --paper)")

    log.info("ouch_session starting")
    log.info(f"FPGA orders  UDP :{LISTEN_PORT}")
    log.info(f"FPGA ACKs    UDP {FPGA_IP}:{ACK_PORT}")
    if args.paper:
        log.info(f"Mode: paper  latency={args.latency}ms")
    else:
        log.info(f"Mode: live  exchange={args.host}:{args.port}  user={args.user.strip()}")

    try:
        asyncio.run(run_session(args))
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    main()
