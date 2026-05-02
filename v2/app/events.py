"""Typed domain events (v2).

Extends v1 events with fields previously dropped from raw WS frames:
- t_exchange: Kalshi's server-side timestamp (for latency measurement)
- sid: subscription ID (correlates sequence numbers)
- seq: sequence number (for gap detection)

t_receipt and t_exchange stay as float (seconds) internally; the silver
writer converts both to int64 nanoseconds at the serialization boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class OrderBookUpdate:
    t_receipt: float
    market_ticker: str
    bid_yes: int
    ask_yes: int
    bid_size: int
    ask_size: int
    t_exchange: float | None = None  # Kalshi server timestamp (None for snapshots)
    sid: int | None = None           # subscription ID
    seq: int | None = None           # sequence number (for gap detection)


@dataclass(frozen=True)
class TradeEvent:
    t_receipt: float
    market_ticker: str
    side: str
    price: int
    size: int
    t_exchange: float | None = None  # Kalshi server timestamp
    sid: int | None = None           # subscription ID
    seq: int | None = None           # sequence number (for gap detection)


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


@dataclass(frozen=True)
class MMReconcileEvent:
    """Emitted when the reconciliation loop detects a state mismatch."""
    t_receipt: float
    market_ticker: str
    field: str                # "position" or "order"
    internal_value: str
    actual_value: str
    action_taken: str         # "corrected" | "cancelled_orphan"


@dataclass(frozen=True)
class MMCircuitBreakerEvent:
    """Emitted when the REST circuit breaker opens or closes."""
    t_receipt: float
    state: str                # "open" | "closed"
    consecutive_failures: int
    last_error: str | None


Event = Union[
    OrderBookUpdate, TradeEvent,
    BookInvalidated, MMQuoteEvent, MMOrderEvent, MMFillEvent,
    MMReconcileEvent, MMCircuitBreakerEvent,
]
