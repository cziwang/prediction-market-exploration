"""Passive market maker on Kalshi KXNBAPTS.

Phase 1: paper trading with PaperOrderClient (no real orders).
Strategy is synchronous and deterministic — all timing uses event.t_receipt,
never wall clock. This guarantees replay fidelity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from app.events import (
    BookInvalidated,
    Event,
    MMCircuitBreakerEvent,
    MMFillEvent,
    MMOrderEvent,
    MMQuoteEvent,
    MMReconcileEvent,
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
    state_path: Path | None = None  # persist positions to JSON file across restarts
    # Scaled per-ticker skew: widen by cents_per_contract * |position|
    skew_cents_per_contract: int = 1  # 0 = legacy fixed-1c skew
    # Position age skew: widen quotes as inventory ages
    age_skew_interval_s: float = 1800.0  # add step_cents per this many seconds held
    age_skew_step_cents: int = 1         # cents to widen per age tier
    max_age_skew_cents: int = 10         # cap on age-based widening
    # Absolute exposure soft limit: suppress new-exposure side above this
    abs_exposure_soft_limit: int = 150   # 0 = disabled
    # Player-level correlated skew: track net position across thresholds for same player
    use_player_skew: bool = True
    player_skew_cents_per_contract: int = 2
    # Minimum volume filter: don't quote until ticker has seen N trades
    min_trades_to_quote: int = 20  # 0 = disabled
    # Queue-aware fill simulation: only fill when trade exceeds estimated queue ahead
    use_queue_model: bool = True
    queue_ahead_cap: int = 2  # max contracts assumed ahead of us (caps raw book depth)
    # Dynamic order sizing
    use_dynamic_sizing: bool = False  # False = fixed order_size
    max_order_size: int = 2           # hard cap on contracts per order
    spread_size_threshold: int = 6    # spread (cents) where size=2 becomes possible
    # Phase 2: live trading
    circuit_breaker_threshold: int = 3  # consecutive REST failures before halt
    reconcile_interval_s: float = 30.0  # REST fallback reconciliation frequency
    pending_timeout_s: float = 5.0      # force-reset pending/cancel_pending after this


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
    client_order_id: str | None = None  # Phase 2: correlate REST → WS ACK
    price: int | None = None
    remaining_size: int = 0
    t_entered: float = 0.0              # Phase 2: when state was entered (for timeout)


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
        queue_ahead: int = 0,
    ) -> str:
        order_id = f"paper-{self._next_id}"
        self._next_id += 1
        self._resting[order_id] = {
            "ticker": ticker,
            "side": side,
            "price": price_cents,
            "remaining": size,
            "queue_ahead": queue_ahead,
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
        use_queue = self._strategy._config.use_queue_model
        for oid in list(self._resting):
            info = self._resting.get(oid)
            if info is None or info["ticker"] != trade.market_ticker:
                continue
            # Buy order filled when taker sells YES (taker_side=="no") at our bid
            if (info["side"] == "bid" and trade.side == "no"
                    and trade.price == info["price"]):
                queue = info.get("queue_ahead", 0) if use_queue else 0
                available = trade.size - queue
                if available <= 0:
                    continue
                fill_size = min(info["remaining"], available)
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
                queue = info.get("queue_ahead", 0) if use_queue else 0
                available = trade.size - queue
                if available <= 0:
                    continue
                fill_size = min(info["remaining"], available)
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
        live: bool = False,
    ) -> None:
        self._client = order_client
        self._config = config or MMConfig()
        self._live = live
        self._positions: dict[str, int] = {}
        self._aggregate_abs_position: int = 0
        self._agg_net_position: int = 0
        # Position age tracking: ticker → t_receipt when position first went non-zero
        self._position_opened_at: dict[str, float] = {}
        # Player-level net positions: player_key → net contracts across all thresholds
        self._player_positions: dict[str, int] = {}
        # Trade count per ticker for minimum volume filter
        self._trade_counts: dict[str, int] = {}
        # Last-seen book mid per ticker for adverse selection measurement
        self._last_mid: dict[str, int] = {}
        # Load persisted positions if state file exists
        if self._config.state_path is not None and self._config.state_path.exists():
            self._load_state()
        # ticker → {"bid": OrderSideState, "ask": OrderSideState}
        self._order_state: dict[str, dict[str, OrderSideState]] = {}
        # Last emitted quote per ticker (for change-detection)
        self._last_quote: dict[str, tuple[int | None, int | None]] = {}
        # Collected strategy events for the caller to emit to silver
        self.pending_events: list[Event] = []

        # Phase 2: correlation tables for WS push channels
        # client_order_id → (ticker, side) — populated at placement,
        # consumed when user_orders ACK arrives
        self._client_order_map: dict[str, tuple[str, str]] = {}
        # order_id → (ticker, side) — populated at ACK,
        # consumed by fill and cancel_ack dispatchers
        self._order_id_map: dict[str, tuple[str, str]] = {}
        # client_order_ids that should be cancelled as soon as ACK arrives
        self._pending_cancel: set[str] = set()
        # Shutdown event for reconciliation loop
        self._shutdown = asyncio.Event()

    # -- async fire-and-forget helpers (live mode) --

    def _schedule_place(
        self, ticker: str, side: str, price: int, size: int, coid: str,
    ) -> None:
        """Schedule an async place_limit call. On failure, reject the order."""
        async def _do() -> None:
            try:
                await self._client.place_limit(
                    ticker, side, price, size, client_order_id=coid,
                )
            except Exception as e:
                log.warning("place_limit failed for %s/%s: %s", ticker, side, e)
                self.on_order_rejected(ticker, side, str(e))
        try:
            asyncio.get_running_loop().create_task(_do())
        except RuntimeError:
            # No running loop (tests) — call sync
            self._client.place_limit(
                ticker, side, price, size, client_order_id=coid,
            )

    def _schedule_cancel(self, order_id: str) -> None:
        """Schedule an async cancel call."""
        async def _do() -> None:
            try:
                await self._client.cancel(order_id)
            except Exception as e:
                log.warning("cancel failed for %s: %s", order_id, e)
        try:
            asyncio.get_running_loop().create_task(_do())
        except RuntimeError:
            # No running loop (tests) — call sync
            self._client.cancel(order_id)

    # -- state persistence --

    def _load_state(self) -> None:
        path = self._config.state_path
        assert path is not None
        with open(path) as f:
            data = json.load(f)
        self._positions = {k: v for k, v in data.get("positions", {}).items() if v != 0}
        self._aggregate_abs_position = sum(abs(v) for v in self._positions.values())
        self._agg_net_position = sum(self._positions.values())
        self._position_opened_at = {
            k: v for k, v in data.get("position_opened_at", {}).items()
            if k in self._positions
        }
        # Rebuild player positions from per-ticker positions
        for ticker, pos in self._positions.items():
            pk = self._player_key(ticker)
            if pk:
                self._player_positions[pk] = self._player_positions.get(pk, 0) + pos
        log.info("loaded %d positions from %s", len(self._positions), path)

    def _save_state(self) -> None:
        path = self._config.state_path
        assert path is not None
        cleaned = {k: v for k, v in self._positions.items() if v != 0}
        opened_at = {k: v for k, v in self._position_opened_at.items() if k in cleaned}
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"positions": cleaned, "position_opened_at": opened_at}, f)
        os.replace(tmp, path)

    # -- public API --

    def on_event(self, event: Event) -> None:
        # Live mode: suppress quoting when circuit breaker is open
        if self._live and hasattr(self._client, 'circuit_open') and self._client.circuit_open:
            if isinstance(event, TradeEvent):
                self._on_trade(event)  # still count trades
            elif isinstance(event, BookInvalidated):
                self._on_book_invalidated(event)  # still clean up
            return

        if isinstance(event, OrderBookUpdate):
            self._on_book_update(event)
        elif isinstance(event, TradeEvent):
            self._on_trade(event)
        elif isinstance(event, BookInvalidated):
            self._on_book_invalidated(event)

    # -- order lifecycle callbacks (called by PaperOrderClient) --

    def on_order_ack(
        self, ticker: str, side: str, order_id: str,
        client_order_id: str | None = None,
    ) -> None:
        state = self._get_side(ticker, side)

        # Consume client_order_id from correlation map
        if client_order_id:
            self._client_order_map.pop(client_order_id, None)

        # Phase 2: check if this order was marked for immediate cancel
        # (e.g., BookInvalidated fired while order was pending)
        if client_order_id and client_order_id in self._pending_cancel:
            self._pending_cancel.discard(client_order_id)
            state.state = "cancel_pending"
            state.order_id = order_id
            self._order_id_map[order_id] = (ticker, side)
            if self._live:
                self._schedule_cancel(order_id)
            else:
                self._client.cancel(ticker, side, order_id, 0.0)
            return

        if state.state == "pending":
            state.state = "resting"
            state.order_id = order_id
            self._order_id_map[order_id] = (ticker, side)

    def on_order_rejected(self, ticker: str, side: str, error: str) -> None:
        state = self._get_side(ticker, side)
        if state.client_order_id:
            self._client_order_map.pop(state.client_order_id, None)
        state.state = "idle"
        state.order_id = None
        state.client_order_id = None
        state.price = None

    def on_cancel_ack(self, ticker: str, side: str) -> None:
        state = self._get_side(ticker, side)
        if state.order_id:
            self._order_id_map.pop(state.order_id, None)
        state.state = "idle"
        state.order_id = None
        state.client_order_id = None
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

        # Position age tracking
        if pos_before == 0 and pos_after != 0:
            self._position_opened_at[ticker] = t
        elif pos_after == 0:
            self._position_opened_at.pop(ticker, None)

        # Player-level position tracking
        pk = self._player_key(ticker)
        if pk:
            self._player_positions[pk] = self._player_positions.get(pk, 0) + delta

        state = self._get_side(ticker, side)
        state.remaining_size = remaining_size
        if remaining_size == 0:
            if state.order_id:
                self._order_id_map.pop(state.order_id, None)
            state.state = "idle"
            state.order_id = None
            state.client_order_id = None
            state.price = None

        if self._config.state_path is not None:
            self._save_state()

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
            book_mid_at_fill=self._last_mid.get(ticker, 0),
        ))

    # -- internal --

    @staticmethod
    def _player_key(ticker: str) -> str | None:
        """Extract player key from ticker for correlated position tracking.

        KXNBAPTS-26APR25DENMIN-DENCBRAUN0-10 → 'DENCBRAUN0'
        """
        parts = ticker.split("-")
        return parts[2] if len(parts) >= 4 else None

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

        # Skip markets at extreme prices — effectively decided, no MM edge
        if update.bid_yes <= 10 or update.ask_yes >= 90:
            # Cancel any resting orders on this dead market
            for side in ("bid", "ask"):
                state = self._get_side(ticker, side)
                if state.state == "resting":
                    state.state = "cancel_pending"
                    if self._live:
                        self._schedule_cancel(state.order_id)
                    else:
                        self._client.cancel(
                            ticker, side, state.order_id or "", update.t_receipt,
                        )
            return
        position = self._positions.get(ticker, 0)

        # Net-of-fee edge check
        mid = (update.bid_yes + update.ask_yes) // 2
        self._last_mid[ticker] = mid
        fee = maker_fee_cents(mid)
        net_half_spread = (spread // 2) - fee

        # Quoting decisions
        reason_no_bid: str | None = None
        reason_no_ask: str | None = None

        if net_half_spread < self._config.min_edge_cents:
            # Allow the offsetting side through on tight spreads to close positions
            if position >= 0:  # flat or long → no need to buy more
                reason_no_bid = "spread_narrow"
            if position <= 0:  # flat or short → no need to sell more
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

        # Minimum volume filter
        if self._config.min_trades_to_quote > 0:
            if self._trade_counts.get(ticker, 0) < self._config.min_trades_to_quote:
                if reason_no_bid is None:
                    reason_no_bid = "low_volume"
                if reason_no_ask is None:
                    reason_no_ask = "low_volume"

        # Absolute exposure soft limit
        if (self._config.abs_exposure_soft_limit > 0
                and self._aggregate_abs_position >= self._config.abs_exposure_soft_limit):
            if position >= 0 and reason_no_bid is None:
                reason_no_bid = "abs_soft_limit"
            if position <= 0 and reason_no_ask is None:
                reason_no_ask = "abs_soft_limit"

        # Skew quotes when carrying inventory
        bid_price = update.bid_yes
        ask_price = update.ask_yes

        # Per-ticker skew (scaled or legacy)
        if self._config.skew_cents_per_contract > 0:
            if abs(position) >= self._config.skew_threshold:
                skew = abs(position) * self._config.skew_cents_per_contract
                if position > 0:
                    bid_price = max(1, bid_price - skew)
                else:
                    ask_price = min(99, ask_price + skew)
        else:
            if position > self._config.skew_threshold:
                bid_price = max(1, bid_price - 1)
            elif position < -self._config.skew_threshold:
                ask_price = min(99, ask_price + 1)

        # Position age skew
        if self._config.age_skew_interval_s > 0 and position != 0:
            opened_at = self._position_opened_at.get(ticker)
            if opened_at is not None:
                age_s = update.t_receipt - opened_at
                age_tiers = int(age_s / self._config.age_skew_interval_s)
                age_skew = min(
                    age_tiers * self._config.age_skew_step_cents,
                    self._config.max_age_skew_cents,
                )
                if age_skew > 0:
                    if position > 0:
                        bid_price = max(1, bid_price - age_skew)
                    else:
                        ask_price = min(99, ask_price + age_skew)

        # Player-level correlated skew
        if self._config.use_player_skew:
            pk = self._player_key(ticker)
            if pk:
                player_pos = self._player_positions.get(pk, 0)
                if player_pos != 0:
                    p_skew = abs(player_pos) * self._config.player_skew_cents_per_contract
                    if player_pos > 0:
                        bid_price = max(1, bid_price - p_skew)
                    else:
                        ask_price = min(99, ask_price + p_skew)

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

        self._maybe_update_side(ticker, "bid", desired_bid, update.t_receipt, update)
        self._maybe_update_side(ticker, "ask", desired_ask, update.t_receipt, update)

    def _on_trade(self, trade: TradeEvent) -> None:
        if not trade.market_ticker.startswith(self._config.series_filter):
            return
        self._trade_counts[trade.market_ticker] = (
            self._trade_counts.get(trade.market_ticker, 0) + 1
        )
        # Live mode: fills come from WS fill channel, not trade-stream matching
        if not self._live:
            self._client.check_fill(trade)

    def _on_book_invalidated(self, event: BookInvalidated) -> None:
        ticker = event.market_ticker
        for side in ("bid", "ask"):
            state = self._get_side(ticker, side)
            if state.state == "resting":
                state.state = "cancel_pending"
                if self._live:
                    self._schedule_cancel(state.order_id)
                else:
                    self._client.cancel(
                        ticker, side, state.order_id or "", event.t_receipt,
                    )
            elif state.state == "pending":
                if self._live and state.client_order_id:
                    # Order may still be in-flight on REST. Track
                    # client_order_id so we cancel on ACK arrival.
                    self._pending_cancel.add(state.client_order_id)
                # Reset to idle (paper: ACKs are instant so safe;
                # live: pending_cancel handles the race)
                state.state = "idle"
                state.order_id = None
                state.price = None
        self._last_quote.pop(ticker, None)

    def _compute_order_size(
        self, ticker: str, side: str, position: int,
        update: OrderBookUpdate,
    ) -> int:
        """Compute order size based on spread, inventory, and position limits."""
        cfg = self._config
        if not cfg.use_dynamic_sizing:
            return cfg.order_size

        spread = update.ask_yes - update.bid_yes
        is_extending = (side == "bid" and position > 0) or (
            side == "ask" and position < 0
        )
        is_reducing = (side == "bid" and position < 0) or (
            side == "ask" and position > 0
        )

        # Spread bonus: linear ramp above threshold, cap at +1 contract
        spread_bonus = max(
            0.0, (spread - cfg.spread_size_threshold) / cfg.spread_size_threshold
        )
        spread_bonus = min(spread_bonus, 1.0)

        # Quadratic inventory penalty when extending position
        penalty = 0.25 * position ** 2 if is_extending else 0.0

        raw = 1.0 + spread_bonus - penalty
        size = max(1, min(int(raw), cfg.max_order_size))

        # Never exceed position limit
        room = cfg.max_position - abs(position)
        size = min(size, max(room, 0))

        # Never overshoot on reducing side
        if is_reducing:
            size = min(size, abs(position))

        return max(1, size)

    def _maybe_update_side(
        self, ticker: str, side: str, price: int | None, t: float,
        update: OrderBookUpdate | None = None,
    ) -> None:
        state = self._get_side(ticker, side)

        if price is None:
            # Want to stop quoting
            if state.state == "resting":
                state.state = "cancel_pending"
                self._client.cancel(ticker, side, state.order_id or "", t)
            return

        if state.state == "idle":
            position = self._positions.get(ticker, 0)
            size = (
                self._compute_order_size(ticker, side, position, update)
                if update is not None
                else self._config.order_size
            )
            state.state = "pending"
            state.price = price
            state.remaining_size = size
            state.t_entered = t
            if self._live:
                coid = str(uuid4())
                state.client_order_id = coid
                self._client_order_map[coid] = (ticker, side)
                self._schedule_place(ticker, side, price, size, coid)
            else:
                # Queue depth: contracts ahead of us, capped
                queue_ahead = 0
                if update is not None:
                    raw_depth = update.bid_size if side == "bid" else update.ask_size
                    queue_ahead = min(raw_depth, self._config.queue_ahead_cap)
                self._client.place_limit(
                    ticker, side, price, size, t, queue_ahead=queue_ahead,
                )
        elif state.state == "resting" and state.price != price:
            # Price changed — cancel, will re-place on next update
            state.state = "cancel_pending"
            if self._live:
                self._schedule_cancel(state.order_id)
            else:
                self._client.cancel(ticker, side, state.order_id or "", t)
        # pending or cancel_pending: wait for ACK

    # -- Phase 2: live trading callbacks --

    def on_ws_fill(
        self, ticker: str, side: str, order_id: str,
        count: int, price_cents: int, fee_cents: int,
        is_taker: bool, post_position: int, t_ms: int,
    ) -> None:
        """Called by WS fill channel. Authoritative — replaces check_fill()."""
        state = self._get_side(ticker, side)

        pos_before = self._positions.get(ticker, 0)
        self._positions[ticker] = post_position
        self._aggregate_abs_position = sum(abs(v) for v in self._positions.values())
        self._agg_net_position = sum(self._positions.values())

        # Position age tracking
        if pos_before == 0 and post_position != 0:
            self._position_opened_at[ticker] = t_ms / 1000.0
        elif post_position == 0:
            self._position_opened_at.pop(ticker, None)

        # Player-level position tracking
        delta = post_position - pos_before
        pk = self._player_key(ticker)
        if pk:
            self._player_positions[pk] = self._player_positions.get(pk, 0) + delta

        # Track remaining size
        state.remaining_size -= count
        remaining = max(0, state.remaining_size)

        if remaining == 0:
            self._order_id_map.pop(order_id, None)
            state.state = "idle"
            state.order_id = None
            state.client_order_id = None
            state.price = None

        if self._config.state_path is not None:
            self._save_state()

        self.pending_events.append(MMFillEvent(
            t_receipt=t_ms / 1000.0,
            market_ticker=ticker,
            side="buy" if side == "bid" else "sell",
            price=price_cents,
            fill_size=count,
            order_remaining_size=remaining,
            position_before=pos_before,
            position_after=post_position,
            maker_fee=fee_cents,
            order_id=order_id,
            book_mid_at_fill=self._last_mid.get(ticker, 0),
        ))

    def on_market_close(self, ticker: str, event_type: str) -> None:
        """Called when market_lifecycle_v2 signals deactivated/determined/settled."""
        for side in ("bid", "ask"):
            state = self._get_side(ticker, side)
            if state.state == "resting":
                state.state = "cancel_pending"
                if self._live:
                    self._schedule_cancel(state.order_id)
                else:
                    self._client.cancel(ticker, side, state.order_id or "", 0.0)
        pos = self._positions.get(ticker, 0)
        if pos != 0:
            log.warning("POSITION AT %s: %s pos=%d", event_type, ticker, pos)

    def on_position_update(self, ticker: str, position: int) -> None:
        """Called by market_positions channel. Sanity check only."""
        internal = self._positions.get(ticker, 0)
        if internal != position:
            log.error(
                "POSITION DRIFT %s: internal=%d kalshi=%d",
                ticker, internal, position,
            )
            old = internal
            self._positions[ticker] = position
            self._aggregate_abs_position = sum(
                abs(v) for v in self._positions.values()
            )
            self._agg_net_position = sum(self._positions.values())
            # Update player positions
            delta = position - old
            pk = self._player_key(ticker)
            if pk:
                self._player_positions[pk] = (
                    self._player_positions.get(pk, 0) + delta
                )
            self.pending_events.append(MMReconcileEvent(
                t_receipt=time.time(),
                market_ticker=ticker,
                field="position",
                internal_value=str(old),
                actual_value=str(position),
                action_taken="corrected",
            ))

    async def bootstrap(self) -> None:
        """Seed internal state from Kalshi's REST API (source of truth).

        Called once at startup before processing any WS events.
        """
        self._positions = await self._client.get_positions()
        api_orders = await self._client.get_open_orders()

        # Cancel any orphaned orders from a prior crash
        for order in api_orders:
            log.info(
                "cancelling orphan order %s from prior session", order.order_id,
            )
            try:
                await self._client.cancel(order.order_id)
            except Exception:
                log.exception("failed to cancel orphan %s", order.order_id)

        # Rebuild derived state from positions
        self._aggregate_abs_position = sum(
            abs(v) for v in self._positions.values()
        )
        self._agg_net_position = sum(self._positions.values())
        for ticker, pos in self._positions.items():
            pk = self._player_key(ticker)
            if pk:
                self._player_positions[pk] = (
                    self._player_positions.get(pk, 0) + pos
                )

        log.info(
            "bootstrap complete: %d positions, %d orphan orders cancelled",
            len(self._positions), len(api_orders),
        )

    async def reconciliation_loop(self) -> None:
        """Periodic truth-check against Kalshi's REST API.

        Safety net behind WS push channels. In steady state finds zero
        mismatches.
        """
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._config.reconcile_interval_s,
                )
                break  # shutdown was set
            except asyncio.TimeoutError:
                pass

            now = time.time()

            # Stale pending/cancel_pending timeout
            for ticker, sides in list(self._order_state.items()):
                for side_name, state in sides.items():
                    if state.state in ("pending", "cancel_pending"):
                        if now - state.t_entered > self._config.pending_timeout_s:
                            log.error(
                                "STALE %s on %s/%s for %.1fs — resetting",
                                state.state, ticker, side_name,
                                now - state.t_entered,
                            )
                            if state.client_order_id:
                                self._pending_cancel.add(state.client_order_id)
                            state.state = "idle"
                            state.order_id = None
                            state.client_order_id = None

            try:
                api_positions = await self._client.get_positions()
                api_orders = await self._client.get_open_orders()
            except Exception as e:
                log.warning("reconciliation failed: %s", e)
                continue

            # Diff positions
            all_tickers = set(list(self._positions) + list(api_positions))
            for ticker in all_tickers:
                internal = self._positions.get(ticker, 0)
                actual = api_positions.get(ticker, 0)
                if internal != actual:
                    log.error(
                        "POSITION MISMATCH %s: internal=%d actual=%d",
                        ticker, internal, actual,
                    )
                    self._positions[ticker] = actual
                    self.pending_events.append(MMReconcileEvent(
                        t_receipt=now,
                        market_ticker=ticker,
                        field="position",
                        internal_value=str(internal),
                        actual_value=str(actual),
                        action_taken="corrected",
                    ))

            # Diff open orders — detect orphans
            known_order_ids = {
                state.order_id
                for sides in self._order_state.values()
                for state in sides.values()
                if state.order_id is not None
            }
            for order in api_orders:
                if order.order_id not in known_order_ids:
                    log.warning(
                        "ORPHAN ORDER %s on %s — cancelling",
                        order.order_id, order.ticker,
                    )
                    try:
                        await self._client.cancel(order.order_id)
                    except Exception:
                        log.exception(
                            "failed to cancel orphan %s", order.order_id,
                        )
                    self.pending_events.append(MMReconcileEvent(
                        t_receipt=now,
                        market_ticker=order.ticker,
                        field="order",
                        internal_value="none",
                        actual_value=order.order_id,
                        action_taken="cancelled_orphan",
                    ))

            self._aggregate_abs_position = sum(
                abs(v) for v in self._positions.values()
            )
            self._agg_net_position = sum(self._positions.values())

    def stop(self) -> None:
        """Signal shutdown for the reconciliation loop."""
        self._shutdown.set()
