"""Passive market maker on Kalshi KXNBAPTS.

Phase 1: paper trading with PaperOrderClient (no real orders).
Strategy is synchronous and deterministic — all timing uses event.t_receipt,
never wall clock. This guarantees replay fidelity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.events import (
    BookInvalidated,
    Event,
    MMFillEvent,
    MMOrderEvent,
    MMQuoteEvent,
    OrderBookUpdate,
    TradeEvent,
)

log = logging.getLogger(__name__)


@dataclass
class MMConfig:
    min_spread_cents: int = 3
    min_edge_cents: int = 1       # net edge (half_spread - fee) floor
    max_position: int = 10        # per-ticker
    max_aggregate_position: int = 200
    skew_threshold: int = 3
    order_size: int = 1
    series_filter: str = "KXNBAPTS-"
    # Aggregate directional skew
    agg_skew_threshold: int = 5   # start widening overexposed side at this |net|
    agg_skew_max: int = 15        # stop quoting overexposed side at this |net|
    agg_skew_step_size: int = 5   # net positions per widening tier
    agg_skew_step_cents: int = 1  # cents to widen per tier


def maker_fee_cents(price_cents: int) -> int:
    """Kalshi maker fee in integer cents. 0.0175 * C * (1-C) scaled to cents."""
    # price_cents is 0-100. fee = 0.0175 * (p/100) * (1 - p/100) * 100 cents
    return max(1, int(0.0175 * price_cents * (100 - price_cents) / 100))


# ---------------------------------------------------------------------------
# Per-ticker order state machine
# ---------------------------------------------------------------------------

@dataclass
class OrderSideState:
    """State for one side (bid or ask) of one ticker.

    States: idle → pending → resting → cancel_pending → idle
    """
    state: str = "idle"
    order_id: str | None = None
    price: int | None = None
    remaining_size: int = 0


# ---------------------------------------------------------------------------
# Paper order client (Phase 1)
# ---------------------------------------------------------------------------

class PaperOrderClient:
    """Simulates order lifecycle without touching Kalshi's API.

    - place_limit(): instant ACK, order tracked as resting
    - cancel(): instant ACK, order removed
    - check_fill(): called on every TradeEvent, matches against resting orders
    """

    def __init__(self, strategy: "MMStrategy") -> None:
        self._strategy = strategy
        self._resting: dict[str, dict] = {}  # order_id → info
        self._next_id: int = 0
        self.order_log: list[dict] = []

    def place_limit(
        self, ticker: str, side: str, price_cents: int, size: int, t: float,
    ) -> str:
        order_id = f"paper-{self._next_id}"
        self._next_id += 1
        self._resting[order_id] = {
            "ticker": ticker,
            "side": side,
            "price": price_cents,
            "remaining": size,
        }
        self.order_log.append({
            "t": t, "action": f"place_{side}", "ticker": ticker,
            "price": price_cents, "size": size, "order_id": order_id,
        })
        # Instant ACK
        self._strategy.on_order_ack(ticker, side, order_id)
        return order_id

    def cancel(self, ticker: str, side: str, order_id: str, t: float) -> None:
        self._resting.pop(order_id, None)
        self.order_log.append({
            "t": t, "action": "cancel", "ticker": ticker,
            "order_id": order_id,
        })
        self._strategy.on_cancel_ack(ticker, side)

    def check_fill(self, trade: TradeEvent) -> None:
        """Match a trade against resting orders. Called on every TradeEvent."""
        for oid in list(self._resting):
            info = self._resting.get(oid)
            if info is None or info["ticker"] != trade.market_ticker:
                continue
            # Buy order filled when taker sells YES (taker_side=="no") at our bid
            if (info["side"] == "bid" and trade.side == "no"
                    and trade.price == info["price"]):
                fill_size = min(info["remaining"], trade.size)
                info["remaining"] -= fill_size
                if info["remaining"] <= 0:
                    del self._resting[oid]
                self._strategy.on_fill(
                    info["ticker"], "bid", fill_size,
                    info["remaining"] if oid in self._resting else 0,
                    info["price"], oid, trade.t_receipt,
                )
                return
            # Sell order filled when taker buys YES (taker_side=="yes") at our ask
            if (info["side"] == "ask" and trade.side == "yes"
                    and trade.price == info["price"]):
                fill_size = min(info["remaining"], trade.size)
                info["remaining"] -= fill_size
                if info["remaining"] <= 0:
                    del self._resting[oid]
                self._strategy.on_fill(
                    info["ticker"], "ask", fill_size,
                    info["remaining"] if oid in self._resting else 0,
                    info["price"], oid, trade.t_receipt,
                )
                return


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class MMStrategy:
    """Passive market maker. Deterministic: uses only event.t_receipt, never wall clock."""

    def __init__(
        self,
        order_client: PaperOrderClient,
        config: MMConfig | None = None,
    ) -> None:
        self._client = order_client
        self._config = config or MMConfig()
        self._positions: dict[str, int] = {}
        self._aggregate_abs_position: int = 0
        self._agg_net_position: int = 0
        # ticker → {"bid": OrderSideState, "ask": OrderSideState}
        self._order_state: dict[str, dict[str, OrderSideState]] = {}
        # Last emitted quote per ticker (for change-detection)
        self._last_quote: dict[str, tuple[int | None, int | None]] = {}
        # Collected strategy events for the caller to emit to silver
        self.pending_events: list[Event] = []

    # -- public API --

    def on_event(self, event: Event) -> None:
        if isinstance(event, OrderBookUpdate):
            self._on_book_update(event)
        elif isinstance(event, TradeEvent):
            self._on_trade(event)
        elif isinstance(event, BookInvalidated):
            self._on_book_invalidated(event)

    # -- order lifecycle callbacks (called by PaperOrderClient) --

    def on_order_ack(self, ticker: str, side: str, order_id: str) -> None:
        state = self._get_side(ticker, side)
        if state.state == "pending":
            state.state = "resting"
            state.order_id = order_id

    def on_order_rejected(self, ticker: str, side: str, error: str) -> None:
        state = self._get_side(ticker, side)
        state.state = "idle"
        state.order_id = None
        state.price = None

    def on_cancel_ack(self, ticker: str, side: str) -> None:
        state = self._get_side(ticker, side)
        state.state = "idle"
        state.order_id = None
        state.price = None

    def on_fill(
        self, ticker: str, side: str, fill_size: int, remaining_size: int,
        price: int, order_id: str, t: float,
    ) -> None:
        pos_before = self._positions.get(ticker, 0)
        delta = fill_size if side == "bid" else -fill_size
        pos_after = pos_before + delta
        self._positions[ticker] = pos_after
        self._aggregate_abs_position = sum(abs(v) for v in self._positions.values())
        self._agg_net_position = sum(self._positions.values())

        state = self._get_side(ticker, side)
        state.remaining_size = remaining_size
        if remaining_size == 0:
            state.state = "idle"
            state.order_id = None
            state.price = None

        self.pending_events.append(MMFillEvent(
            t_receipt=t,
            market_ticker=ticker,
            side="buy" if side == "bid" else "sell",
            price=price,
            fill_size=fill_size,
            order_remaining_size=remaining_size,
            position_before=pos_before,
            position_after=pos_after,
            maker_fee=maker_fee_cents(price),
            order_id=order_id,
            book_mid_at_fill=0,  # not available in paper mode
        ))

    # -- internal --

    def _get_side(self, ticker: str, side: str) -> OrderSideState:
        if ticker not in self._order_state:
            self._order_state[ticker] = {
                "bid": OrderSideState(),
                "ask": OrderSideState(),
            }
        return self._order_state[ticker][side]

    def _agg_skew_adjustment(self) -> tuple[int, int, bool, bool]:
        """Aggregate directional skew: (bid_adj, ask_adj, suppress_bid, suppress_ask).

        bid_adj/ask_adj: cents to widen (positive = less aggressive).
        suppress_bid/suppress_ask: stop quoting that side entirely.
        """
        net = self._agg_net_position
        cfg = self._config
        bid_adj, ask_adj = 0, 0
        suppress_bid, suppress_ask = False, False

        if net <= -cfg.agg_skew_max:
            suppress_ask = True
        elif net <= -cfg.agg_skew_threshold:
            steps = (-net - cfg.agg_skew_threshold) // cfg.agg_skew_step_size + 1
            ask_adj = steps * cfg.agg_skew_step_cents

        if net >= cfg.agg_skew_max:
            suppress_bid = True
        elif net >= cfg.agg_skew_threshold:
            steps = (net - cfg.agg_skew_threshold) // cfg.agg_skew_step_size + 1
            bid_adj = steps * cfg.agg_skew_step_cents

        return bid_adj, ask_adj, suppress_bid, suppress_ask

    def _on_book_update(self, update: OrderBookUpdate) -> None:
        ticker = update.market_ticker
        if not ticker.startswith(self._config.series_filter):
            return

        spread = update.ask_yes - update.bid_yes
        if spread <= 0:
            return
        position = self._positions.get(ticker, 0)

        # Net-of-fee edge check
        mid = (update.bid_yes + update.ask_yes) // 2
        fee = maker_fee_cents(mid)
        net_half_spread = (spread // 2) - fee

        # Quoting decisions
        reason_no_bid: str | None = None
        reason_no_ask: str | None = None

        if net_half_spread < self._config.min_edge_cents:
            reason_no_bid = "spread_narrow"
            reason_no_ask = "spread_narrow"
        if position >= self._config.max_position:
            reason_no_bid = "pos_limit"
        if position <= -self._config.max_position:
            reason_no_ask = "pos_limit"
        if self._aggregate_abs_position >= self._config.max_aggregate_position:
            if reason_no_bid is None:
                reason_no_bid = "agg_limit"
            if reason_no_ask is None:
                reason_no_ask = "agg_limit"

        should_bid = reason_no_bid is None
        should_ask = reason_no_ask is None

        # Skew quotes when carrying inventory
        bid_price = update.bid_yes
        ask_price = update.ask_yes
        if position > self._config.skew_threshold:
            bid_price = max(1, bid_price - 1)
        elif position < -self._config.skew_threshold:
            ask_price = min(99, ask_price + 1)

        # Aggregate directional skew
        bid_adj, ask_adj, suppress_bid, suppress_ask = self._agg_skew_adjustment()
        if suppress_bid:
            reason_no_bid = reason_no_bid or "agg_skew"
        elif bid_adj > 0:
            bid_price = max(1, bid_price - bid_adj)
        if suppress_ask:
            reason_no_ask = reason_no_ask or "agg_skew"
        elif ask_adj > 0:
            ask_price = min(99, ask_price + ask_adj)

        should_bid = reason_no_bid is None
        should_ask = reason_no_ask is None

        desired_bid = bid_price if should_bid else None
        desired_ask = ask_price if should_ask else None

        # Emit MMQuoteEvent only when the decision changes
        last = self._last_quote.get(ticker)
        if last != (desired_bid, desired_ask):
            self._last_quote[ticker] = (desired_bid, desired_ask)
            self.pending_events.append(MMQuoteEvent(
                t_receipt=update.t_receipt,
                market_ticker=ticker,
                bid_price=desired_bid,
                ask_price=desired_ask,
                book_bid=update.bid_yes,
                book_ask=update.ask_yes,
                spread=spread,
                position=position,
                reason_no_bid=reason_no_bid,
                reason_no_ask=reason_no_ask,
            ))

        self._maybe_update_side(ticker, "bid", desired_bid, update.t_receipt)
        self._maybe_update_side(ticker, "ask", desired_ask, update.t_receipt)

    def _on_trade(self, trade: TradeEvent) -> None:
        if not trade.market_ticker.startswith(self._config.series_filter):
            return
        self._client.check_fill(trade)

    def _on_book_invalidated(self, event: BookInvalidated) -> None:
        ticker = event.market_ticker
        for side in ("bid", "ask"):
            state = self._get_side(ticker, side)
            if state.state == "resting":
                self._client.cancel(
                    ticker, side, state.order_id or "", event.t_receipt,
                )
            elif state.state == "pending":
                # No order resting yet — just reset to idle
                state.state = "idle"
                state.order_id = None
                state.price = None
        self._last_quote.pop(ticker, None)

    def _maybe_update_side(
        self, ticker: str, side: str, price: int | None, t: float,
    ) -> None:
        state = self._get_side(ticker, side)

        if price is None:
            # Want to stop quoting
            if state.state == "resting":
                state.state = "cancel_pending"
                self._client.cancel(ticker, side, state.order_id or "", t)
            return

        if state.state == "idle":
            state.state = "pending"
            state.price = price
            state.remaining_size = self._config.order_size
            self._client.place_limit(
                ticker, side, price, self._config.order_size, t,
            )
        elif state.state == "resting" and state.price != price:
            # Price changed — cancel, will re-place on next update
            state.state = "cancel_pending"
            self._client.cancel(ticker, side, state.order_id or "", t)
        # pending or cancel_pending: wait for ACK
