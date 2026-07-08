"""Enriched trade output model (DESIGN.md §3): one row per Kalshi trade,
carrying both the receipt-time view (what our system knew) and the
event-time view (what had actually happened on court)."""

from pydantic import BaseModel, ConfigDict


class EnrichedTrade(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = 1

    # --- Trade ---
    t_trade_receipt_ns: int
    t_trade_exchange_ns: int | None
    market_ticker: str
    series: str
    trade_price: int  # YES price, cents
    trade_size_cc: int  # centi-contracts
    taker_side: str
    game_id: str

    # --- Receipt-time view: "what did we know?" ---
    r_period: int | None = None
    r_seconds_remaining: float | None = None
    r_score_diff: int | None = None
    r_model_prob: float | None = None  # P(YES) per model, on received game state
    r_edge: float | None = None
    r_info_delay_ms: int | None = None  # trade receipt - game state receipt

    # --- Event-time view: "what had actually happened?" ---
    e_period: int | None = None
    e_seconds_remaining: float | None = None
    e_score_diff: int | None = None
    e_model_prob: float | None = None
    e_edge: float | None = None
    e_info_delay_ms: int | None = None  # trade exchange time - on-court event time

    # --- Common ---
    market_implied_prob: float
