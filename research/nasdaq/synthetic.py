#!/usr/bin/env python3
"""
synthetic.py - Synthetic ITCH 5.0 message generator.

Generates a realistic stream of ITCH messages for a single symbol.
Tracks internal order state to ensure bids never cross asks.
When mid drifts, stale orders on the wrong side are cancelled first.

Usage:
    from synthetic import generate_ticks
    for msg in generate_ticks("AAPL", start_price=185.0, n_events=100_000):
        book.apply(msg)
"""

import random
from typing import Generator

from .itch_parser import AddOrder, OrderExecuted, OrderDelete, SystemEvent


def generate_ticks(
    symbol:       str   = "AAPL",
    start_price:  float = 185.0,
    n_events:     int   = 50_000,
    tick_size:    float = 0.01,
    spread_ticks: int   = 4,
    volatility:   float = 0.00008,
    seed:         int   = 42,
) -> Generator:
    """
    Yields ITCH message objects simulating a realistic order book.

    Maintains internal bid/ask dicts so it can cancel crossing orders
    before adding new ones -- mirrors how real market makers behave.
    """
    rng = random.Random(seed)

    yield SystemEvent(timestamp_ns=0, event_code='Q')

    ts_ns    = 34_200_000_000_000
    next_ref = 1_000_000

    bids: dict = {}   # price -> (ref, shares)
    asks: dict = {}

    mid = start_price

    def new_ref():
        nonlocal next_ref
        next_ref += 1
        return next_ref

    def snap(p):
        return round(round(p / tick_size) * tick_size, 4)

    def best_bid():
        return max(bids) if bids else -1e9

    def best_ask():
        return min(asks) if asks else 1e9

    for _ in range(n_events):
        ts_ns += rng.randint(500_000, 20_000_000)
        mid *= (1.0 + rng.gauss(0, volatility))
        mid  = max(mid, tick_size * 2)

        target_bid = snap(mid - spread_ticks * tick_size / 2)
        target_ask = snap(mid + spread_ticks * tick_size / 2)
        if target_ask <= target_bid:
            target_ask = snap(target_bid + tick_size)

        # Cancel stale orders on wrong side of current mid
        for p in list(bids):
            if p >= target_ask:
                ref, _ = bids.pop(p)
                yield OrderDelete(ts_ns, ref)
        for p in list(asks):
            if p <= target_bid:
                ref, _ = asks.pop(p)
                yield OrderDelete(ts_ns, ref)

        action = rng.random()

        if action < 0.35:
            offset = rng.randint(0, 3) * tick_size
            price  = snap(target_bid - offset)
            if price < best_ask() and price not in bids:
                shares = rng.randint(1, 20) * 100
                ref = new_ref()
                bids[price] = (ref, shares)
                yield AddOrder(ts_ns, ref, 'B', shares, symbol, price)

        elif action < 0.70:
            offset = rng.randint(0, 3) * tick_size
            price  = snap(target_ask + offset)
            if price > best_bid() and price not in asks:
                shares = rng.randint(1, 20) * 100
                ref = new_ref()
                asks[price] = (ref, shares)
                yield AddOrder(ts_ns, ref, 'S', shares, symbol, price)

        elif action < 0.80:
            if bids:
                bp = best_bid()
                ref, shares = bids[bp]
                exec_qty = min(shares, rng.randint(1, 5) * 100)
                yield OrderExecuted(ts_ns, ref, exec_qty, new_ref())
                remaining = shares - exec_qty
                if remaining <= 0:
                    del bids[bp]
                else:
                    bids[bp] = (ref, remaining)

        elif action < 0.90:
            if asks:
                ap = best_ask()
                ref, shares = asks[ap]
                exec_qty = min(shares, rng.randint(1, 5) * 100)
                yield OrderExecuted(ts_ns, ref, exec_qty, new_ref())
                remaining = shares - exec_qty
                if remaining <= 0:
                    del asks[ap]
                else:
                    asks[ap] = (ref, remaining)

        else:
            pool = ([(p, r) for p, (r, _) in bids.items() if p < target_bid - 3*tick_size] +
                    [(p, r) for p, (r, _) in asks.items() if p > target_ask + 3*tick_size])
            if pool:
                p, ref = rng.choice(pool)
                bids.pop(p, None)
                asks.pop(p, None)
                yield OrderDelete(ts_ns, ref)

    yield SystemEvent(timestamp_ns=ts_ns, event_code='C')
