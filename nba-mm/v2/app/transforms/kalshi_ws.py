"""Kalshi WS frame → typed events + depth rows.

Stateful: maintains per-ticker BookState and produces:
- TradeEvent, BookInvalidated (as Event dataclasses)
- OrderBookDepth rows (as pre-formatted dicts)

Integer cents throughout — no floats for prices or sizes.

Note: Kalshi's orderbook_delta channel DOES emit negative deltas for
matched fills.  Trades therefore do NOT modify BookState — the
corresponding delta message handles the book update.  Verified
empirically in v2/notebooks/delta_fill_test.ipynb (2026-05-03).
"""

from __future__ import annotations

import logging
from typing import Optional

from v2.app.core.book_state import BookState, extract_depth_row
from v2.app.core.conversions import dollars_to_cents, parse_ts
from v2.app.events import BookInvalidated, Event, TradeEvent, TransformResult

log = logging.getLogger(__name__)


class KalshiTransform:
    """Raw WS frame → TransformResult (events + depth rows)."""

    def __init__(self) -> None:
        self._books: dict[str, BookState] = {}
        self._conn_id: str | None = None

    def __call__(
        self,
        frame: dict,
        t_receipt: float,
        conn_id: Optional[str] = None,
    ) -> TransformResult:
        events: list[Event] = []
        depth_rows: list[dict] = []

        if conn_id is not None and conn_id != self._conn_id:
            for ticker in list(self._books):
                events.append(BookInvalidated(t_receipt=t_receipt, market_ticker=ticker))
            self._books.clear()
            self._conn_id = conn_id

        msg_type = frame.get("type")
        sid = frame.get("sid")
        seq = frame.get("seq")

        if msg_type == "orderbook_snapshot":
            msg = frame.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return TransformResult(events, depth_rows)
            book = BookState.from_snapshot(msg)
            book.seq = seq
            book.sid = sid
            self._books[ticker] = book
            t_receipt_ns = int(t_receipt * 1_000_000_000)
            depth_rows.append(extract_depth_row(
                book, t_receipt_ns, None, ticker, seq, sid,
            ))

        elif msg_type == "orderbook_delta":
            msg = frame.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return TransformResult(events, depth_rows)
            book = self._books.get(ticker)
            if book is None:
                return TransformResult(events, depth_rows)
            # Skip deltas from a stale subscription — after a re-snapshot
            # creates a new sid, old-sid deltas still arrive and would
            # corrupt the book if applied.
            if sid is not None and book.sid is not None and sid != book.sid:
                return TransformResult(events, depth_rows)
            price_cents = dollars_to_cents(msg["price_dollars"])
            delta = int(round(float(msg["delta_fp"])))
            side = msg["side"]
            book.apply_delta(price_cents, delta, side)
            book.seq = seq

            # If the book has crossed (drift from missed messages or stale
            # seeded state), invalidate it rather than emitting bad data.
            # A future snapshot will re-establish correct state.
            spread = book.spread
            if spread is not None and spread < 0:
                del self._books[ticker]
                events.append(BookInvalidated(t_receipt=t_receipt, market_ticker=ticker))
                return TransformResult(events, depth_rows)

            t_exchange = parse_ts(msg.get("ts"))
            t_receipt_ns = int(t_receipt * 1_000_000_000)
            t_exchange_ns = int(t_exchange * 1_000_000_000) if t_exchange else None
            depth_rows.append(extract_depth_row(
                book, t_receipt_ns, t_exchange_ns, ticker, seq, sid,
            ))

        elif msg_type == "trade":
            msg = frame.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return TransformResult(events, depth_rows)
            yes_price = msg.get("yes_price_dollars")
            count_fp = msg.get("count_fp")
            taker_side = msg.get("taker_side")
            t_exchange = parse_ts(msg.get("ts"))
            if yes_price is not None and count_fp is not None and taker_side:
                yes_price_cents = dollars_to_cents(str(yes_price))
                fill_size = int(round(float(count_fp)))
                events.append(TradeEvent(
                    t_receipt=t_receipt,
                    market_ticker=ticker,
                    side=taker_side,
                    price=yes_price_cents,
                    size=fill_size,
                    t_exchange=t_exchange,
                    sid=sid,
                    seq=seq,
                ))
                # No book update here — the orderbook_delta channel
                # emits a negative delta for the filled quantity, so
                # the delta handler will update the book.

        return TransformResult(events, depth_rows)
