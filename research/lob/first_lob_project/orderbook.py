from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Tuple


Price = Decimal
Size = Decimal
Level = Tuple[str, str]


@dataclass
class OrderBook:
    bids: Dict[Price, Size] = field(default_factory=dict)
    asks: Dict[Price, Size] = field(default_factory=dict)
    last_update_id: int | None = None

    def load_snapshot(
        self,
        bids: Iterable[Level],
        asks: Iterable[Level],
        last_update_id: int,
    ) -> None:
        self.bids.clear()
        self.asks.clear()

        for price_str, size_str in bids:
            price = Decimal(price_str)
            size = Decimal(size_str)
            if size > 0:
                self.bids[price] = size

        for price_str, size_str in asks:
            price = Decimal(price_str)
            size = Decimal(size_str)
            if size > 0:
                self.asks[price] = size

        self.last_update_id = last_update_id

    def apply_absolute_updates(
        self,
        bid_updates: Iterable[Level],
        ask_updates: Iterable[Level],
        final_update_id: int,
    ) -> None:
        for price_str, size_str in bid_updates:
            self._apply_one(self.bids, price_str, size_str)

        for price_str, size_str in ask_updates:
            self._apply_one(self.asks, price_str, size_str)

        self.last_update_id = final_update_id

    @staticmethod
    def _apply_one(side: Dict[Price, Size], price_str: str, size_str: str) -> None:
        price = Decimal(price_str)
        size = Decimal(size_str)

        if size == 0:
            side.pop(price, None)
        else:
            side[price] = size

    def best_bid(self) -> Tuple[Price | None, Size | None]:
        if not self.bids:
            return None, None
        p = max(self.bids)
        return p, self.bids[p]

    def best_ask(self) -> Tuple[Price | None, Size | None]:
        if not self.asks:
            return None, None
        p = min(self.asks)
        return p, self.asks[p]

    def top_n_bids(self, n: int) -> List[Tuple[Price, Size]]:
        return sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:n]

    def top_n_asks(self, n: int) -> List[Tuple[Price, Size]]:
        return sorted(self.asks.items(), key=lambda x: x[0])[:n]

    def midprice(self) -> Decimal | None:
        bid, _ = self.best_bid()
        ask, _ = self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    def spread(self) -> Decimal | None:
        bid, _ = self.best_bid()
        ask, _ = self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def top_of_book_imbalance(self) -> Decimal | None:
        _, bid_size = self.best_bid()
        _, ask_size = self.best_ask()
        if bid_size is None or ask_size is None:
            return None
        denom = bid_size + ask_size
        if denom == 0:
            return Decimal("0")
        return (bid_size - ask_size) / denom