"""Kalshi market events (DESIGN.md §2)."""

from typing import Literal

from pm.core.events import MarketEvent


class TradeEvent(MarketEvent):
    """An executed trade on a Kalshi market."""

    market_ticker: str
    price_cents: int  # YES price, integer cents
    size_cc: int  # centi-contracts (D9)
    taker_side: Literal["yes", "no"]
    t_exchange_ns: int | None = None  # when Kalshi's exchange matched this trade


class BookUpdateEvent(MarketEvent):
    """A single order book level change on a Kalshi market."""

    market_ticker: str
    side: Literal["yes", "no"]
    price_cents: int
    delta_cc: int  # signed size change at this level, centi-contracts (D9)
    seq: int  # per-subscription sequence number (gap detection)
    sid: int  # subscription id (correlates seq)
