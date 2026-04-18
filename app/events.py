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


Event = Union[ScoreEvent, PeriodChange, OrderBookUpdate, TradeEvent]
