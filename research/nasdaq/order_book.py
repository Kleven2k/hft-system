#!/usr/bin/env python3
"""
order_book.py — Order book reconstructor for NASDAQ ITCH 5.0 data.

Processes the stream of ITCH messages and maintains a live order book:
  - Full order table (order_ref -> price, side, shares)
  - Bid side: price levels sorted descending (best bid = highest price)
  - Ask side: price levels sorted ascending  (best ask = lowest price)

After each message, the best bid and ask are accessible as:
    book.best_bid   -- (price, total_shares) or None
    book.best_ask   -- (price, total_shares) or None

Usage:
    from itch_parser import parse_file
    from order_book import OrderBook

    book = OrderBook("AAPL")
    for msg in parse_file(path, symbol_filter="AAPL"):
        book.apply(msg)
        if book.best_bid and book.best_ask:
            print(f"AAPL  bid={book.best_bid[0]:.4f}  ask={book.best_ask[0]:.4f}")
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from .itch_parser import (
    AddOrder, AddOrderMPID, OrderExecuted, OrderExecutedWithPrice,
    OrderCancel, OrderDelete, OrderReplace, Trade, SystemEvent,
)


# ---------------------------------------------------------------------------
# Internal order record
# ---------------------------------------------------------------------------

@dataclass
class Order:
    order_ref: int
    side:      str   # 'B' or 'S'
    price:     float
    shares:    int   # remaining shares


# ---------------------------------------------------------------------------
# Tick snapshot — emitted after every book-changing event
# ---------------------------------------------------------------------------

@dataclass
class BookTick:
    timestamp_ns: int
    best_bid:     Optional[float]  # None if no bids
    best_ask:     Optional[float]  # None if no asks
    bid_size:     int              # total shares at best bid
    ask_size:     int              # total shares at best ask
    last_trade:   Optional[float]  # price of most recent trade
    event:        str              # "ADD", "EXEC", "CANCEL", "DELETE", "REPLACE", "TRADE"


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------

class OrderBook:
    """
    Reconstructs a price-level order book from ITCH 5.0 messages.

    Only tracks orders for self.symbol — orders for other symbols
    are ignored (they won't appear if symbol_filter was set in parse_file).

    Price levels stored as dicts:
        _bids: {price: total_shares}  (buy side)
        _asks: {price: total_shares}  (sell side)

    Inserting/removing from these dicts is O(1).
    Finding best bid/ask is O(n_levels) but cached after each update.
    """

    def __init__(self, symbol: str):
        self.symbol     = symbol
        self._orders: dict[int, Order] = {}         # order_ref -> Order
        self._bids:   dict[float, int] = defaultdict(int)   # price -> total shares
        self._asks:   dict[float, int] = defaultdict(int)

        self.best_bid: Optional[tuple[float, int]] = None   # (price, shares)
        self.best_ask: Optional[tuple[float, int]] = None

        self.last_trade:  Optional[float] = None
        self.timestamp_ns: int = 0

        # Statistics
        self.n_adds     = 0
        self.n_executes = 0
        self.n_cancels  = 0
        self.n_deletes  = 0
        self.n_replaces = 0
        self.n_trades   = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def apply(self, msg) -> Optional[BookTick]:
        """
        Apply one ITCH message.  Returns a BookTick if the book changed,
        None otherwise (e.g. SystemEvent).
        """
        if isinstance(msg, (AddOrder, AddOrderMPID)):
            return self._add(msg)
        if isinstance(msg, OrderExecuted):
            return self._execute(msg)
        if isinstance(msg, OrderExecutedWithPrice):
            return self._execute_price(msg)
        if isinstance(msg, OrderCancel):
            return self._cancel(msg)
        if isinstance(msg, OrderDelete):
            return self._delete(msg)
        if isinstance(msg, OrderReplace):
            return self._replace(msg)
        if isinstance(msg, Trade):
            return self._trade(msg)
        return None   # SystemEvent etc.

    def spread(self) -> Optional[float]:
        """Best ask - best bid in dollars. None if either side is empty."""
        if self.best_bid and self.best_ask:
            return self.best_ask[0] - self.best_bid[0]
        return None

    def mid(self) -> Optional[float]:
        """Mid-price in dollars."""
        if self.best_bid and self.best_ask:
            return (self.best_bid[0] + self.best_ask[0]) / 2.0
        return None

    def spread_bps(self) -> Optional[float]:
        """Spread in basis points (1 bp = 0.01%)."""
        s = self.spread()
        m = self.mid()
        if s is not None and m and m > 0:
            return s / m * 10_000
        return None

    # ------------------------------------------------------------------
    # Private: message handlers
    # ------------------------------------------------------------------

    def _add(self, msg) -> BookTick:
        order = Order(msg.order_ref, msg.side, msg.price, msg.shares)
        self._orders[msg.order_ref] = order
        if msg.side == 'B':
            self._bids[msg.price] += msg.shares
        else:
            self._asks[msg.price] += msg.shares
        self._update_best()
        self.n_adds += 1
        self.timestamp_ns = msg.timestamp_ns
        return self._tick("ADD")

    def _execute(self, msg: OrderExecuted) -> Optional[BookTick]:
        order = self._orders.get(msg.order_ref)
        if order is None:
            return None   # order for different symbol, already gone
        order.shares -= msg.executed_shares
        self._remove_shares(order.side, order.price, msg.executed_shares)
        if order.shares <= 0:
            del self._orders[msg.order_ref]
        self._update_best()
        self.last_trade = order.price
        self.n_executes += 1
        self.timestamp_ns = msg.timestamp_ns
        return self._tick("EXEC")

    def _execute_price(self, msg: OrderExecutedWithPrice) -> Optional[BookTick]:
        order = self._orders.get(msg.order_ref)
        if order is None:
            return None
        order.shares -= msg.executed_shares
        self._remove_shares(order.side, order.price, msg.executed_shares)
        if order.shares <= 0:
            del self._orders[msg.order_ref]
        self._update_best()
        if msg.printable:
            self.last_trade = msg.price
        self.n_executes += 1
        self.timestamp_ns = msg.timestamp_ns
        return self._tick("EXEC")

    def _cancel(self, msg: OrderCancel) -> Optional[BookTick]:
        order = self._orders.get(msg.order_ref)
        if order is None:
            return None
        order.shares -= msg.cancelled_shares
        self._remove_shares(order.side, order.price, msg.cancelled_shares)
        if order.shares <= 0:
            del self._orders[msg.order_ref]
        self._update_best()
        self.n_cancels += 1
        self.timestamp_ns = msg.timestamp_ns
        return self._tick("CANCEL")

    def _delete(self, msg: OrderDelete) -> Optional[BookTick]:
        order = self._orders.pop(msg.order_ref, None)
        if order is None:
            return None
        self._remove_shares(order.side, order.price, order.shares)
        self._update_best()
        self.n_deletes += 1
        self.timestamp_ns = msg.timestamp_ns
        return self._tick("DELETE")

    def _replace(self, msg: OrderReplace) -> Optional[BookTick]:
        # Replace = delete old + add new (possibly different price/size)
        old = self._orders.pop(msg.original_ref, None)
        if old is not None:
            self._remove_shares(old.side, old.price, old.shares)
        # New order inherits the same side as the old one
        side = old.side if old else 'B'
        new_order = Order(msg.new_ref, side, msg.price, msg.shares)
        self._orders[msg.new_ref] = new_order
        if side == 'B':
            self._bids[msg.price] += msg.shares
        else:
            self._asks[msg.price] += msg.shares
        self._update_best()
        self.n_replaces += 1
        self.timestamp_ns = msg.timestamp_ns
        return self._tick("REPLACE")

    def _trade(self, msg: Trade) -> BookTick:
        # Trade messages are not against visible book orders — just record price
        self.last_trade = msg.price
        self.n_trades += 1
        self.timestamp_ns = msg.timestamp_ns
        return self._tick("TRADE")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _remove_shares(self, side: str, price: float, shares: int) -> None:
        levels = self._bids if side == 'B' else self._asks
        if price in levels:
            levels[price] -= shares
            if levels[price] <= 0:
                del levels[price]

    def _update_best(self) -> None:
        if self._bids:
            bp = max(self._bids)
            self.best_bid = (bp, self._bids[bp])
        else:
            self.best_bid = None
        if self._asks:
            ap = min(self._asks)
            self.best_ask = (ap, self._asks[ap])
        else:
            self.best_ask = None

    def _tick(self, event: str) -> BookTick:
        return BookTick(
            timestamp_ns = self.timestamp_ns,
            best_bid     = self.best_bid[0] if self.best_bid else None,
            best_ask     = self.best_ask[0] if self.best_ask else None,
            bid_size     = self.best_bid[1] if self.best_bid else 0,
            ask_size     = self.best_ask[1] if self.best_ask else 0,
            last_trade   = self.last_trade,
            event        = event,
        )

    def __repr__(self) -> str:
        bid = f"${self.best_bid[0]:.4f} x{self.best_bid[1]}" if self.best_bid else "---"
        ask = f"${self.best_ask[0]:.4f} x{self.best_ask[1]}" if self.best_ask else "---"
        spd = f"{self.spread_bps():.1f}bp" if self.spread_bps() else "---"
        return (f"OrderBook({self.symbol})  bid={bid}  ask={ask}  spread={spd}"
                f"  orders={len(self._orders)}")
