"""Typed domain events.

Every downstream component (live strategy, backtest replay, silver writer)
consumes these objects. Raw JSON from NBA / Kalshi is translated into these
by `app.transforms` exactly once per event, inside the live process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class ScoreEvent:
    t_receipt: float
    game_id: str
    action_number: int
    period: int
    clock: str
    home: int
    away: int
    scoring_team: str | None
    points: int


@dataclass(frozen=True)
class PeriodChange:
    t_receipt: float
    game_id: str
    new_period: int


@dataclass(frozen=True)
class OrderBookUpdate:
    t_receipt: float
    market_ticker: str
    bid_yes: int
    ask_yes: int
    bid_size: int
    ask_size: int


@dataclass(frozen=True)
class TradeEvent:
    t_receipt: float
    market_ticker: str
    side: str
    price: int
    size: int


@dataclass(frozen=True)
class BookInvalidated:
    """Emitted by the transform when a WS reconnect invalidates a ticker's book."""
    t_receipt: float
    market_ticker: str


@dataclass(frozen=True)
class MMQuoteEvent:
    """Strategy quoting decision. Emitted only when the decision changes."""
    t_receipt: float
    market_ticker: str
    bid_price: int | None
    ask_price: int | None
    book_bid: int
    book_ask: int
    spread: int
    position: int
    reason_no_bid: str | None
    reason_no_ask: str | None


@dataclass(frozen=True)
class MMOrderEvent:
    """Every order placed or cancelled (paper or live)."""
    t_receipt: float
    market_ticker: str
    action: str       # place_bid | place_ask | cancel
    price: int | None
    size: int | None
    order_id: str | None
    reason: str
    error: str | None


@dataclass(frozen=True)
class MMFillEvent:
    """A confirmed fill (supports partial fills)."""
    t_receipt: float
    market_ticker: str
    side: str
    price: int
    fill_size: int
    order_remaining_size: int
    position_before: int
    position_after: int
    maker_fee: int
    order_id: str
    book_mid_at_fill: int


Event = Union[
    ScoreEvent, PeriodChange, OrderBookUpdate, TradeEvent,
    BookInvalidated, MMQuoteEvent, MMOrderEvent, MMFillEvent,
]
