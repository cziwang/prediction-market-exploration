"""Kalshi WS frame → typed events.

Stateful transform: maintains per-ticker order book state and emits
OrderBookUpdate / TradeEvent / BookInvalidated. Called synchronously
on the ingester's task per data-flow.md rules.

Integer cents throughout — no floats for prices or sizes.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.events import BookInvalidated, Event, OrderBookUpdate, TradeEvent

log = logging.getLogger(__name__)

# Sizes below this (in cents) are floating-point artifacts from accumulated deltas.
MIN_SIZE = 50  # 0.5 dollars — well below any real order


class OrderBookState:
    """In-memory order book for one ticker. Dict[int, int] = price_cents → size_cents."""

    __slots__ = ("yes_book", "no_book", "_min_size")

    def __init__(self) -> None:
        self.yes_book: dict[int, int] = {}
        self.no_book: dict[int, int] = {}
        self._min_size: int = MIN_SIZE

    @classmethod
    def from_snapshot(cls, msg: dict, min_size: int = MIN_SIZE) -> "OrderBookState":
        book = cls()
        book._min_size = min_size
        for price_str, size_str in msg.get("yes_dollars_fp", []):
            p = _dollars_to_cents(price_str)
            s = int(round(float(size_str)))
            if s >= min_size:
                book.yes_book[p] = s
        for price_str, size_str in msg.get("no_dollars_fp", []):
            p = _dollars_to_cents(price_str)
            s = int(round(float(size_str)))
            if s >= min_size:
                book.no_book[p] = s
        return book

    def apply_delta(self, msg: dict) -> None:
        price_cents = _dollars_to_cents(msg["price_dollars"])
        delta_cents = int(round(float(msg["delta_fp"])))
        side = msg["side"]
        book = self.yes_book if side == "yes" else self.no_book
        min_size = self._min_size
        new_size = book.get(price_cents, 0) + delta_cents
        if new_size < min_size:
            book.pop(price_cents, None)
        else:
            book[price_cents] = new_size

    @property
    def best_bid(self) -> int | None:
        return max(self.yes_book) if self.yes_book else None

    @property
    def best_ask(self) -> int | None:
        if not self.no_book:
            return None
        return 100 - max(self.no_book)

    @property
    def bid_size_top(self) -> int:
        if not self.yes_book:
            return 0
        return self.yes_book[max(self.yes_book)]

    @property
    def ask_size_top(self) -> int:
        if not self.no_book:
            return 0
        return self.no_book[max(self.no_book)]

    @property
    def mid(self) -> int | None:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return (b + a) // 2


def _dollars_to_cents(s: str) -> int:
    """'0.5200' → 52. Handles Kalshi's 4-decimal dollar strings."""
    return int(round(float(s) * 100))


class KalshiTransform:
    """Raw WS frame → list of typed Events.

    Tracks conn_id: on connection change, emits BookInvalidated for every
    tracked ticker and clears all book state.
    """

    def __init__(self) -> None:
        self._books: dict[str, OrderBookState] = {}
        self._conn_id: str | None = None

    def __call__(
        self,
        frame: dict,
        t_receipt: float,
        conn_id: Optional[str] = None,
    ) -> list[Event]:
        events: list[Event] = []

        # Connection change → invalidate all books
        if conn_id is not None and conn_id != self._conn_id:
            for ticker in list(self._books):
                events.append(BookInvalidated(t_receipt=t_receipt, market_ticker=ticker))
            self._books.clear()
            self._conn_id = conn_id

        msg_type = frame.get("type")

        if msg_type == "orderbook_snapshot":
            msg = frame.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return events
            self._books[ticker] = OrderBookState.from_snapshot(msg)
            book = self._books[ticker]
            if book.best_bid is not None and book.best_ask is not None:
                events.append(OrderBookUpdate(
                    t_receipt=t_receipt,
                    market_ticker=ticker,
                    bid_yes=book.best_bid,
                    ask_yes=book.best_ask,
                    bid_size=book.bid_size_top,
                    ask_size=book.ask_size_top,
                ))

        elif msg_type == "orderbook_delta":
            msg = frame.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return events
            book = self._books.get(ticker)
            if book is None:
                return events  # delta without snapshot — skip
            book.apply_delta(msg)
            if book.best_bid is not None and book.best_ask is not None:
                events.append(OrderBookUpdate(
                    t_receipt=t_receipt,
                    market_ticker=ticker,
                    bid_yes=book.best_bid,
                    ask_yes=book.best_ask,
                    bid_size=book.bid_size_top,
                    ask_size=book.ask_size_top,
                ))

        elif msg_type == "trade":
            msg = frame.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return events
            yes_price = msg.get("yes_price_dollars")
            count_fp = msg.get("count_fp")
            taker_side = msg.get("taker_side")
            if yes_price is not None and count_fp is not None and taker_side:
                events.append(TradeEvent(
                    t_receipt=t_receipt,
                    market_ticker=ticker,
                    side=taker_side,
                    price=_dollars_to_cents(str(yes_price)),
                    size=int(round(float(count_fp))),
                ))

        return events
