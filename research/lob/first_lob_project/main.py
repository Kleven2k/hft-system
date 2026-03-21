from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List

import aiohttp

from config import (
    DATA_DIR,
    PARQUET_FLUSH_ROWS,
    REST_DEPTH_URL,
    REST_SYMBOL,
    SNAPSHOT_LIMIT,
    WS_URL,
)
from orderbook import OrderBook
from storage import ParquetWriter


async def fetch_snapshot(session: aiohttp.ClientSession) -> Dict[str, Any]:
    params = {"symbol": REST_SYMBOL, "limit": SNAPSHOT_LIMIT}
    async with session.get(REST_DEPTH_URL, params=params, timeout=10) as resp:
        resp.raise_for_status()
        return await resp.json()


async def ws_reader(
    ws: aiohttp.ClientWebSocketResponse,
    event_buffer: Deque[Dict[str, Any]],
) -> None:
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            event = json.loads(msg.data)
            if event.get("e") == "depthUpdate":
                event_buffer.append(event)
        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            break


async def synchronize_book(
    session: aiohttp.ClientSession,
    event_buffer: Deque[Dict[str, Any]],
    book: OrderBook,
    max_attempts: int = 20,
) -> None:
    """
    Robust Binance Spot sync with a live websocket buffer.

    Key idea:
    - Fetch a snapshot that is not older than the earliest buffered event.
    - If the snapshot is slightly ahead of the current buffer, WAIT for more events.
    - Then drop stale events and bridge snapshot -> stream.
    """
    for attempt in range(1, max_attempts + 1):
        print(f"[sync {attempt}] Waiting for websocket buffer...")

        while len(event_buffer) < 50:
            await asyncio.sleep(0.05)

        # Use a stable copy of current buffer
        buffered = list(event_buffer)
        first_buffered_U = buffered[0]["U"]

        print(
            f"[sync {attempt}] Initial buffer size={len(buffered)} "
            f"first U={first_buffered_U} last u={buffered[-1]['u']}"
        )

        # Snapshot must not be older than the first buffered event
        while True:
            snapshot = await fetch_snapshot(session)
            snapshot_last_id = snapshot["lastUpdateId"]

            if snapshot_last_id < first_buffered_U:
                print(
                    f"[sync {attempt}] Snapshot too old "
                    f"(snapshot={snapshot_last_id} < first U={first_buffered_U}). Refetching..."
                )
                await asyncio.sleep(0.1)
                continue
            break

        print(f"[sync {attempt}] Using snapshot lastUpdateId={snapshot_last_id}")

        # IMPORTANT:
        # If snapshot is ahead of current buffered data, do NOT restart.
        # Wait until websocket reader has buffered events beyond snapshot_last_id.
        wait_loops = 0
        while not event_buffer or event_buffer[-1]["u"] <= snapshot_last_id:
            wait_loops += 1
            if wait_loops % 10 == 0:
                last_u = event_buffer[-1]["u"] if event_buffer else None
                print(
                    f"[sync {attempt}] Waiting for buffer to advance past snapshot... "
                    f"snapshot={snapshot_last_id}, current last u={last_u}"
                )
            await asyncio.sleep(0.05)

        # Re-copy buffer now that it extends beyond snapshot
        buffered = list(event_buffer)

        print(
            f"[sync {attempt}] Buffer now extends past snapshot: "
            f"first U={buffered[0]['U']} last u={buffered[-1]['u']}"
        )

        # Drop events already covered by the snapshot
        buffered = [e for e in buffered if e["u"] > snapshot_last_id]

        if not buffered:
            print(f"[sync {attempt}] No buffered events after drop. Retrying...")
            await asyncio.sleep(0.1)
            continue

        first_event = buffered[0]

        # Binance bridge condition
        if not (first_event["U"] <= snapshot_last_id + 1 <= first_event["u"]):
            print(
                f"[sync {attempt}] First remaining event does not bridge snapshot correctly: "
                f"U={first_event['U']} u={first_event['u']} snapshot={snapshot_last_id}. Retrying..."
            )
            await asyncio.sleep(0.1)
            continue

        # Load snapshot only after successful overlap check
        book.load_snapshot(
            bids=snapshot["bids"],
            asks=snapshot["asks"],
            last_update_id=snapshot_last_id,
        )

        prev_u = snapshot_last_id

        try:
            for event in buffered:
                if event["u"] <= prev_u:
                    continue

                if event["U"] > prev_u + 1:
                    raise RuntimeError(
                        f"Gap during buffered apply: expected <= {prev_u + 1}, got U={event['U']}"
                    )

                book.apply_absolute_updates(
                    bid_updates=event["b"],
                    ask_updates=event["a"],
                    final_update_id=event["u"],
                )
                prev_u = event["u"]

        except RuntimeError as e:
            print(f"[sync {attempt}] {e}. Retrying...")
            await asyncio.sleep(0.1)
            continue

        # Prune already-applied events from the live buffer
        while event_buffer and event_buffer[0]["u"] <= book.last_update_id:
            event_buffer.popleft()

        print(f"[sync {attempt}] Synchronization successful. last_update_id={book.last_update_id}")
        return

    raise RuntimeError("Failed to synchronize local order book after multiple attempts.")

def build_row(book: OrderBook, event_time_ms: int) -> Dict[str, Any] | None:
    best_bid, best_bid_size = book.best_bid()
    best_ask, best_ask_size = book.best_ask()

    if best_bid is None or best_ask is None:
        return None

    spread = book.spread()
    mid = book.midprice()
    imbalance = book.top_of_book_imbalance()

    return {
        "event_time": datetime.fromtimestamp(event_time_ms / 1000, tz=timezone.utc).isoformat(),
        "capture_time": datetime.now(timezone.utc).isoformat(),
        "best_bid": float(best_bid),
        "best_bid_size": float(best_bid_size),
        "best_ask": float(best_ask),
        "best_ask_size": float(best_ask_size),
        "spread": float(spread) if spread is not None else None,
        "midprice": float(mid) if mid is not None else None,
        "top_imbalance": float(imbalance) if imbalance is not None else None,
    }

async def stream_book() -> None:
    book = OrderBook()
    writer = ParquetWriter(DATA_DIR)
    row_buffer: List[Dict[str, Any]] = []
    event_buffer: Deque[Dict[str, Any]] = deque(maxlen=50000)

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL, heartbeat=30) as ws:
            print("Connected. Starting websocket reader...")
            reader_task = asyncio.create_task(ws_reader(ws, event_buffer))

            try:
                print("Synchronizing local order book...")
                await synchronize_book(session, event_buffer, book)
                print("Synchronized. Streaming updates...")

                expected_prev_u = book.last_update_id

                while True:
                    if not event_buffer:
                        await asyncio.sleep(0.01)
                        if reader_task.done():
                            break
                        continue

                    event = event_buffer.popleft()
                    U = event["U"]
                    u = event["u"]

                    if expected_prev_u is not None and u <= expected_prev_u:
                        continue

                    if expected_prev_u is not None and U > expected_prev_u + 1:
                        print(
                            f"Sequence gap detected during live stream "
                            f"(expected <= {expected_prev_u + 1}, got U={U}). Re-synchronizing..."
                        )
                        await synchronize_book(session, event_buffer, book)
                        expected_prev_u = book.last_update_id
                        continue

                    book.apply_absolute_updates(
                        bid_updates=event["b"],
                        ask_updates=event["a"],
                        final_update_id=u,
                    )
                    expected_prev_u = u

                    row = build_row(book, event["E"])
                    if row is not None:
                        row_buffer.append(row)

                    if len(row_buffer) >= PARQUET_FLUSH_ROWS:
                        writer.append_rows(row_buffer)
                        print(
                            f"Flushed {len(row_buffer)} rows. "
                            f"Best bid={row['best_bid']} best ask={row['best_ask']} spread={row['spread']}"
                        )
                        row_buffer.clear()

            finally:
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader_task

    if row_buffer:
        writer.append_rows(row_buffer)
        print(f"Final flush: {len(row_buffer)} rows")


if __name__ == "__main__":
    import contextlib

    try:
        asyncio.run(stream_book())
    except KeyboardInterrupt:
        print("Stopped by user.")