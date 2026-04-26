"""Tests for Phase 2 live trading additions to app.strategy.mm."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

from app.events import (
    BookInvalidated,
    MMFillEvent,
    MMReconcileEvent,
    OrderBookUpdate,
)
from app.strategy.mm import MMConfig, MMStrategy, OrderSideState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LIVE_DEFAULTS = dict(
    skew_cents_per_contract=0,
    age_skew_interval_s=0.0,
    abs_exposure_soft_limit=0,
    use_player_skew=False,
    min_trades_to_quote=0,
    use_queue_model=False,
    queue_ahead_cap=0,
    use_dynamic_sizing=False,
    reconcile_interval_s=0.1,  # fast for tests
    pending_timeout_s=0.1,
)


class FakeOrderClient:
    """Mimics KalshiOrderClient interface for testing live mode.

    Unlike PaperOrderClient, does NOT deliver instant ACKs — the test
    must call strategy.on_order_ack() explicitly (simulating WS push).
    """

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.circuit_open: bool = False

    def place_limit(
        self, ticker: str, side: str, price_cents: int, size: int,
        client_order_id: str | None = None,
    ) -> None:
        self.placed.append({
            "ticker": ticker, "side": side, "price": price_cents,
            "size": size, "client_order_id": client_order_id,
        })

    def cancel(self, order_id: str) -> None:
        self.cancelled.append(order_id)

    async def get_positions(self) -> dict[str, int]:
        return {}

    async def get_open_orders(self, ticker: str | None = None) -> list:
        return []


@dataclass
class FakeOpenOrder:
    order_id: str
    client_order_id: str | None
    ticker: str
    side: str
    action: str
    price_cents: int
    remaining: int


def _make_live_strategy(**config_overrides) -> tuple[MMStrategy, FakeOrderClient]:
    merged = {**_LIVE_DEFAULTS, **config_overrides}
    config = MMConfig(**merged)
    client = FakeOrderClient()
    strategy = MMStrategy(order_client=client, config=config, live=True)  # type: ignore[arg-type]
    return strategy, client


def _book_update(ticker: str, bid: int, ask: int, t: float = 1.0) -> OrderBookUpdate:
    return OrderBookUpdate(
        t_receipt=t, market_ticker=ticker,
        bid_yes=bid, ask_yes=ask,
        bid_size=10000, ask_size=10000,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClientOrderIdCorrelation:
    """Verify client_order_id is generated and tracked in live mode."""

    def test_place_generates_client_order_id(self):
        s, c = _make_live_strategy()
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        assert len(c.placed) == 2
        for p in c.placed:
            assert p["client_order_id"] is not None
            assert p["client_order_id"] in s._client_order_map

    def test_ack_populates_order_id_map(self):
        s, c = _make_live_strategy()
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        coid_bid = c.placed[0]["client_order_id"]
        coid_ask = c.placed[1]["client_order_id"]

        s.on_order_ack("KXNBAPTS-TEST", "bid", "oid-1", client_order_id=coid_bid)
        s.on_order_ack("KXNBAPTS-TEST", "ask", "oid-2", client_order_id=coid_ask)

        assert s._order_id_map["oid-1"] == ("KXNBAPTS-TEST", "bid")
        assert s._order_id_map["oid-2"] == ("KXNBAPTS-TEST", "ask")
        # client_order_map should be consumed
        assert coid_bid not in s._client_order_map
        assert coid_ask not in s._client_order_map


class TestPendingCancel:
    """BookInvalidated while order is pending should cancel on ACK."""

    def test_pending_cancel_on_book_invalidated(self):
        s, c = _make_live_strategy()
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        coid_bid = c.placed[0]["client_order_id"]

        # Book invalidated while bid is pending
        s.on_event(BookInvalidated(t_receipt=2.0, market_ticker="KXNBAPTS-TEST"))
        assert coid_bid in s._pending_cancel

        # ACK arrives — should immediately cancel
        s.on_order_ack("KXNBAPTS-TEST", "bid", "oid-1", client_order_id=coid_bid)
        assert "oid-1" in c.cancelled
        assert coid_bid not in s._pending_cancel

        # State should be cancel_pending (waiting for cancel ACK)
        state = s._get_side("KXNBAPTS-TEST", "bid")
        assert state.state == "cancel_pending"


class TestWsFill:
    """Test on_ws_fill — authoritative fill from WS fill channel."""

    def test_ws_fill_updates_position(self):
        s, c = _make_live_strategy()
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        coid = c.placed[0]["client_order_id"]

        # ACK the bid
        s.on_order_ack("KXNBAPTS-TEST", "bid", "oid-1", client_order_id=coid)

        # WS fill
        s.on_ws_fill(
            ticker="KXNBAPTS-TEST", side="bid", order_id="oid-1",
            count=1, price_cents=45, fee_cents=1,
            is_taker=False, post_position=1, t_ms=3000,
        )

        assert s._positions["KXNBAPTS-TEST"] == 1
        assert s._aggregate_abs_position == 1

        # State should be idle (fully filled)
        state = s._get_side("KXNBAPTS-TEST", "bid")
        assert state.state == "idle"
        assert "oid-1" not in s._order_id_map

    def test_ws_fill_emits_fill_event(self):
        s, c = _make_live_strategy()
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        coid = c.placed[0]["client_order_id"]
        s.on_order_ack("KXNBAPTS-TEST", "bid", "oid-1", client_order_id=coid)
        s.pending_events.clear()

        s.on_ws_fill(
            ticker="KXNBAPTS-TEST", side="bid", order_id="oid-1",
            count=1, price_cents=45, fee_cents=2,
            is_taker=False, post_position=1, t_ms=3000,
        )

        fills = [e for e in s.pending_events if isinstance(e, MMFillEvent)]
        assert len(fills) == 1
        assert fills[0].maker_fee == 2  # authoritative from Kalshi
        assert fills[0].position_after == 1

    def test_ws_fill_partial(self):
        s, c = _make_live_strategy(order_size=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        coid = c.placed[0]["client_order_id"]
        s.on_order_ack("KXNBAPTS-TEST", "bid", "oid-1", client_order_id=coid)

        # Partial fill: 1 of 3
        s.on_ws_fill(
            ticker="KXNBAPTS-TEST", side="bid", order_id="oid-1",
            count=1, price_cents=45, fee_cents=1,
            is_taker=False, post_position=1, t_ms=3000,
        )

        state = s._get_side("KXNBAPTS-TEST", "bid")
        assert state.state == "resting"  # still resting
        assert state.remaining_size == 2
        assert "oid-1" in s._order_id_map


class TestMarketClose:
    """Test on_market_close cancels resting orders."""

    def test_market_close_cancels_resting(self):
        s, c = _make_live_strategy()
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        coid_bid = c.placed[0]["client_order_id"]
        coid_ask = c.placed[1]["client_order_id"]
        s.on_order_ack("KXNBAPTS-TEST", "bid", "oid-1", client_order_id=coid_bid)
        s.on_order_ack("KXNBAPTS-TEST", "ask", "oid-2", client_order_id=coid_ask)

        s.on_market_close("KXNBAPTS-TEST", "determined")

        assert "oid-1" in c.cancelled
        assert "oid-2" in c.cancelled
        assert s._get_side("KXNBAPTS-TEST", "bid").state == "cancel_pending"
        assert s._get_side("KXNBAPTS-TEST", "ask").state == "cancel_pending"


class TestPositionUpdate:
    """Test on_position_update — sanity check from market_positions channel."""

    def test_position_drift_corrected(self):
        s, c = _make_live_strategy()
        s._positions["KXNBAPTS-TEST"] = 3
        s._aggregate_abs_position = 3

        s.on_position_update("KXNBAPTS-TEST", 5)

        assert s._positions["KXNBAPTS-TEST"] == 5
        assert s._aggregate_abs_position == 5
        reconciles = [e for e in s.pending_events if isinstance(e, MMReconcileEvent)]
        assert len(reconciles) == 1
        assert reconciles[0].field == "position"
        assert reconciles[0].internal_value == "3"
        assert reconciles[0].actual_value == "5"

    def test_position_match_no_event(self):
        s, c = _make_live_strategy()
        s._positions["KXNBAPTS-TEST"] = 3
        s.on_position_update("KXNBAPTS-TEST", 3)
        assert len(s.pending_events) == 0


class TestCircuitBreaker:
    """Test that circuit breaker suppresses quoting."""

    def test_circuit_open_suppresses_quotes(self):
        s, c = _make_live_strategy()
        c.circuit_open = True
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        assert len(c.placed) == 0

    def test_circuit_closed_allows_quotes(self):
        s, c = _make_live_strategy()
        c.circuit_open = False
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        assert len(c.placed) == 2


class TestBootstrap:
    """Test bootstrap() seeds positions and cancels orphans."""

    def test_bootstrap_seeds_positions(self):
        s, c = _make_live_strategy()

        async def mock_get_positions():
            return {"KXNBAPTS-A": 2, "KXNBAPTS-B": -1}

        c.get_positions = mock_get_positions
        c.get_open_orders = AsyncMock(return_value=[])

        asyncio.get_event_loop().run_until_complete(s.bootstrap())

        assert s._positions == {"KXNBAPTS-A": 2, "KXNBAPTS-B": -1}
        assert s._aggregate_abs_position == 3

    def test_bootstrap_cancels_orphans(self):
        s, c = _make_live_strategy()
        c.get_positions = AsyncMock(return_value={})

        orphan = FakeOpenOrder(
            order_id="orphan-1", client_order_id=None,
            ticker="KXNBAPTS-X", side="yes", action="buy",
            price_cents=50, remaining=1,
        )
        c.get_open_orders = AsyncMock(return_value=[orphan])
        c.cancel = AsyncMock()

        asyncio.get_event_loop().run_until_complete(s.bootstrap())
        c.cancel.assert_called_once_with("orphan-1")


class TestReconciliationLoop:
    """Test the reconciliation loop detects mismatches."""

    def test_reconcile_corrects_position_mismatch(self):
        s, c = _make_live_strategy()
        s._positions["KXNBAPTS-A"] = 3

        async def mock_get_positions():
            return {"KXNBAPTS-A": 5}

        c.get_positions = mock_get_positions
        c.get_open_orders = AsyncMock(return_value=[])

        async def run():
            # Start loop, let it run one cycle, then stop
            task = asyncio.create_task(s.reconciliation_loop())
            await asyncio.sleep(0.2)
            s.stop()
            await task

        asyncio.get_event_loop().run_until_complete(run())

        assert s._positions["KXNBAPTS-A"] == 5
        reconciles = [e for e in s.pending_events if isinstance(e, MMReconcileEvent)]
        assert len(reconciles) >= 1
        assert reconciles[0].field == "position"

    def test_reconcile_cancels_orphan_orders(self):
        s, c = _make_live_strategy()

        c.get_positions = AsyncMock(return_value={})

        orphan = FakeOpenOrder(
            order_id="orphan-99", client_order_id=None,
            ticker="KXNBAPTS-Y", side="yes", action="buy",
            price_cents=40, remaining=1,
        )
        c.get_open_orders = AsyncMock(return_value=[orphan])
        c.cancel = AsyncMock()

        async def run():
            task = asyncio.create_task(s.reconciliation_loop())
            await asyncio.sleep(0.2)
            s.stop()
            await task

        asyncio.get_event_loop().run_until_complete(run())

        c.cancel.assert_called_with("orphan-99")
        reconciles = [e for e in s.pending_events if isinstance(e, MMReconcileEvent)]
        orphan_events = [e for e in reconciles if e.field == "order"]
        assert len(orphan_events) >= 1

    def test_stale_pending_timeout(self):
        s, c = _make_live_strategy(pending_timeout_s=0.05)
        # Put a side into pending with old timestamp
        state = s._get_side("KXNBAPTS-STALE", "bid")
        state.state = "pending"
        state.client_order_id = "stale-coid"
        state.t_entered = 0.0  # very old

        c.get_positions = AsyncMock(return_value={})
        c.get_open_orders = AsyncMock(return_value=[])

        async def run():
            task = asyncio.create_task(s.reconciliation_loop())
            await asyncio.sleep(0.2)
            s.stop()
            await task

        asyncio.get_event_loop().run_until_complete(run())

        assert state.state == "idle"
        assert "stale-coid" in s._pending_cancel


class TestLiveTradeSkipsCheckFill:
    """In live mode, on_trade should NOT call check_fill."""

    def test_trade_does_not_simulate_fill(self):
        s, c = _make_live_strategy()
        # Manually set a position and resting order
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        coid = c.placed[0]["client_order_id"]
        s.on_order_ack("KXNBAPTS-TEST", "bid", "oid-1", client_order_id=coid)

        from app.events import TradeEvent
        trade = TradeEvent(
            t_receipt=2.0, market_ticker="KXNBAPTS-TEST",
            side="no", price=45, size=1,
        )
        s.on_event(trade)

        # Position should NOT change (no simulated fill)
        assert s._positions.get("KXNBAPTS-TEST", 0) == 0


class TestOrderIdMapCleanup:
    """Verify _order_id_map is cleaned up on cancel_ack and full fill."""

    def test_cancel_ack_cleans_order_id_map(self):
        s, c = _make_live_strategy()
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        coid = c.placed[0]["client_order_id"]
        s.on_order_ack("KXNBAPTS-TEST", "bid", "oid-1", client_order_id=coid)
        assert "oid-1" in s._order_id_map

        s.on_cancel_ack("KXNBAPTS-TEST", "bid")
        assert "oid-1" not in s._order_id_map

    def test_rejected_cleans_client_order_map(self):
        s, c = _make_live_strategy()
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        coid = c.placed[0]["client_order_id"]

        # Simulate rejection
        state = s._get_side("KXNBAPTS-TEST", "bid")
        state.client_order_id = coid
        s.on_order_rejected("KXNBAPTS-TEST", "bid", "market closed")
        assert coid not in s._client_order_map
