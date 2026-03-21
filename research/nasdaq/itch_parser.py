#!/usr/bin/env python3
"""
itch_parser.py — NASDAQ TotalView-ITCH 5.0 binary message parser.

ITCH 5.0 is the protocol NASDAQ uses for its full market data feed.
Every order added, executed, cancelled, or replaced is broadcast as a
binary UDP message. This parser decodes those messages.

File format:
  The historical data files from NASDAQ wrap each message with a 2-byte
  big-endian length prefix:
      [length: 2 bytes][message: length bytes][length: 2 bytes][message]...

  Files are gzip-compressed (.gz).

Key message types for order book reconstruction:
  A  Add Order (no MPID)
  F  Add Order (with MPID)
  E  Order Executed
  C  Order Executed with Price
  X  Order Cancel (partial)
  D  Order Delete (full)
  U  Order Replace
  P  Trade (non-cross, informational)
  S  System Event (session start/end)

Price encoding:
  All prices are integers in units of 1/10000 of a dollar.
  e.g. price=1234560 means $123.456 → divide by 10000.

Reference:
  docs/specs/NQTVITCHspecification.pdf
"""

import gzip
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

# ---------------------------------------------------------------------------
# Price helper
# ---------------------------------------------------------------------------

PRICE_FACTOR = 10_000  # ITCH prices are in 1/10000th of a dollar


def to_dollars(raw_price: int) -> float:
    return raw_price / PRICE_FACTOR


# ---------------------------------------------------------------------------
# Message dataclasses — one per ITCH message type we care about
# ---------------------------------------------------------------------------

@dataclass
class SystemEvent:
    """S — marks the start/end of a trading session."""
    timestamp_ns: int   # nanoseconds past midnight
    event_code:   str   # 'O'=start, 'S'=start of system, 'Q'=start of market,
                        # 'M'=end of market, 'E'=end of system, 'C'=close

@dataclass
class AddOrder:
    """A — a new limit order has been added to the book."""
    timestamp_ns: int
    order_ref:    int   # unique order identifier
    side:         str   # 'B' = buy, 'S' = sell
    shares:       int
    stock:        str   # ticker symbol (padded to 8 chars, stripped)
    price:        float # dollars

@dataclass
class AddOrderMPID:
    """F — same as AddOrder but includes Market Participant ID."""
    timestamp_ns: int
    order_ref:    int
    side:         str
    shares:       int
    stock:        str
    price:        float
    attribution:  str   # MPID (4 chars)

@dataclass
class OrderExecuted:
    """E — shares of an existing order have been executed."""
    timestamp_ns:   int
    order_ref:      int
    executed_shares: int
    match_number:   int

@dataclass
class OrderExecutedWithPrice:
    """C — execution at a price different from the order's limit price."""
    timestamp_ns:   int
    order_ref:      int
    executed_shares: int
    match_number:   int
    printable:      bool
    price:          float

@dataclass
class OrderCancel:
    """X — a portion of an order has been cancelled."""
    timestamp_ns:     int
    order_ref:        int
    cancelled_shares: int

@dataclass
class OrderDelete:
    """D — an order has been fully removed from the book."""
    timestamp_ns: int
    order_ref:    int

@dataclass
class OrderReplace:
    """U — an order has been replaced with a new order at a new price/size."""
    timestamp_ns:   int
    original_ref:   int
    new_ref:        int
    shares:         int
    price:          float

@dataclass
class Trade:
    """P — a non-cross trade has occurred (not from a visible order)."""
    timestamp_ns: int
    order_ref:    int
    side:         str
    shares:       int
    stock:        str
    price:        float
    match_number: int


# ---------------------------------------------------------------------------
# Parser functions — one per message type
# All unpack big-endian (>) binary data per the ITCH 5.0 spec
# ---------------------------------------------------------------------------

def _ts6(data: bytes, offset: int) -> int:
    """Unpack a 6-byte big-endian timestamp (no native struct format for 6 bytes)."""
    hi, lo = struct.unpack_from(">HI", data, offset)
    return (hi << 32) | lo


def parse_system_event(data: bytes) -> SystemEvent:
    # S: type(1) locate(2) tracking(2) timestamp(6) event_code(1)  = 12 bytes
    ts = _ts6(data, 5)
    event_code = chr(data[11])
    return SystemEvent(timestamp_ns=ts, event_code=event_code)


def parse_add_order(data: bytes) -> AddOrder:
    # A: type(1) locate(2) tracking(2) ts(6) ref(8) side(1) shares(4) stock(8) price(4) = 36
    ts        = _ts6(data, 5)
    order_ref = struct.unpack_from(">Q", data, 11)[0]
    side      = chr(data[19])
    shares    = struct.unpack_from(">I", data, 20)[0]
    stock     = data[24:32].decode("ascii").strip()
    price     = to_dollars(struct.unpack_from(">I", data, 32)[0])
    return AddOrder(ts, order_ref, side, shares, stock, price)


def parse_add_order_mpid(data: bytes) -> AddOrderMPID:
    # F: same as A + attribution(4) = 40 bytes
    ts        = _ts6(data, 5)
    order_ref = struct.unpack_from(">Q", data, 11)[0]
    side      = chr(data[19])
    shares    = struct.unpack_from(">I", data, 20)[0]
    stock     = data[24:32].decode("ascii").strip()
    price     = to_dollars(struct.unpack_from(">I", data, 32)[0])
    attribution = data[36:40].decode("ascii").strip()
    return AddOrderMPID(ts, order_ref, side, shares, stock, price, attribution)


def parse_order_executed(data: bytes) -> OrderExecuted:
    # E: type(1) locate(2) tracking(2) ts(6) ref(8) shares(4) match(8) = 31
    ts        = _ts6(data, 5)
    order_ref = struct.unpack_from(">Q", data, 11)[0]
    shares    = struct.unpack_from(">I", data, 19)[0]
    match     = struct.unpack_from(">Q", data, 23)[0]
    return OrderExecuted(ts, order_ref, shares, match)


def parse_order_executed_price(data: bytes) -> OrderExecutedWithPrice:
    # C: type(1) locate(2) tracking(2) ts(6) ref(8) shares(4) match(8) printable(1) price(4) = 36
    ts        = _ts6(data, 5)
    order_ref = struct.unpack_from(">Q", data, 11)[0]
    shares    = struct.unpack_from(">I", data, 19)[0]
    match     = struct.unpack_from(">Q", data, 23)[0]
    printable = chr(data[31]) == 'Y'
    price     = to_dollars(struct.unpack_from(">I", data, 32)[0])
    return OrderExecutedWithPrice(ts, order_ref, shares, match, printable, price)


def parse_order_cancel(data: bytes) -> OrderCancel:
    # X: type(1) locate(2) tracking(2) ts(6) ref(8) cancelled(4) = 23
    ts        = _ts6(data, 5)
    order_ref = struct.unpack_from(">Q", data, 11)[0]
    cancelled = struct.unpack_from(">I", data, 19)[0]
    return OrderCancel(ts, order_ref, cancelled)


def parse_order_delete(data: bytes) -> OrderDelete:
    # D: type(1) locate(2) tracking(2) ts(6) ref(8) = 19
    ts        = _ts6(data, 5)
    order_ref = struct.unpack_from(">Q", data, 11)[0]
    return OrderDelete(ts, order_ref)


def parse_order_replace(data: bytes) -> OrderReplace:
    # U: type(1) locate(2) tracking(2) ts(6) orig_ref(8) new_ref(8) shares(4) price(4) = 35
    ts       = _ts6(data, 5)
    orig_ref = struct.unpack_from(">Q", data, 11)[0]
    new_ref  = struct.unpack_from(">Q", data, 19)[0]
    shares   = struct.unpack_from(">I", data, 27)[0]
    price    = to_dollars(struct.unpack_from(">I", data, 31)[0])
    return OrderReplace(ts, orig_ref, new_ref, shares, price)


def parse_trade(data: bytes) -> Trade:
    # P: type(1) locate(2) tracking(2) ts(6) ref(8) side(1) shares(4) stock(8) price(4) match(8) = 44
    ts        = _ts6(data, 5)
    order_ref = struct.unpack_from(">Q", data, 11)[0]
    side      = chr(data[19])
    shares    = struct.unpack_from(">I", data, 20)[0]
    stock     = data[24:32].decode("ascii").strip()
    price     = to_dollars(struct.unpack_from(">I", data, 32)[0])
    match     = struct.unpack_from(">Q", data, 36)[0]
    return Trade(ts, order_ref, side, shares, stock, price, match)


# ---------------------------------------------------------------------------
# Dispatch table — message type byte → parser function
# ---------------------------------------------------------------------------

_PARSERS = {
    0x53: parse_system_event,         # S
    0x41: parse_add_order,            # A
    0x46: parse_add_order_mpid,       # F
    0x45: parse_order_executed,       # E
    0x43: parse_order_executed_price, # C
    0x58: parse_order_cancel,         # X
    0x44: parse_order_delete,         # D
    0x55: parse_order_replace,        # U
    0x50: parse_trade,                # P
}

# Message types we intentionally skip (not needed for order book or backtest)
_SKIP = {0x52, 0x48, 0x59, 0x4C, 0x56, 0x57, 0x4B, 0x4A, 0x68, 0x51, 0x42, 0x49, 0x4E}


# ---------------------------------------------------------------------------
# File reader — yields parsed messages from a .gz ITCH file
# ---------------------------------------------------------------------------

def parse_file(
    path: Path,
    symbol_filter: Optional[str] = None,
    max_messages: int = 0,
) -> Generator:
    """
    Parse a gzip-compressed ITCH 5.0 file, yielding message objects.

    path           -- path to the .gz ITCH file
    symbol_filter  -- if set, only yield messages for this ticker (e.g. "AAPL")
    max_messages   -- stop after this many messages (0 = no limit)

    Yields message dataclass objects (AddOrder, OrderExecuted, etc.)

    Example:
        for msg in parse_file(Path("01302019.NASDAQ_ITCH50.gz"), "AAPL"):
            print(msg)
    """
    n = 0
    opener = gzip.open if str(path).endswith(".gz") else open

    with opener(path, "rb") as f:
        while True:
            # Read 2-byte length prefix
            length_bytes = f.read(2)
            if len(length_bytes) < 2:
                break   # end of file

            length = struct.unpack(">H", length_bytes)[0]
            data   = f.read(length)
            if len(data) < length:
                break   # truncated

            msg_type = data[0]

            # Skip message types we don't handle
            if msg_type in _SKIP:
                continue

            parser = _PARSERS.get(msg_type)
            if parser is None:
                continue

            try:
                msg = parser(data)
            except struct.error:
                continue   # malformed message

            # Symbol filter: AddOrder/Trade have a stock field
            if symbol_filter:
                stock = getattr(msg, "stock", None)
                if stock is not None and stock != symbol_filter:
                    continue
                # For order-level messages (Execute, Cancel, Delete, Replace),
                # the caller's order book handles filtering by order_ref.

            yield msg
            n += 1
            if max_messages and n >= max_messages:
                break
