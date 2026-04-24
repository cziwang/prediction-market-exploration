"""Tests for app.strategy.mm."""

from app.events import BookInvalidated, MMFillEvent, MMQuoteEvent, OrderBookUpdate, TradeEvent
from app.strategy.mm import MMConfig, MMStrategy, PaperOrderClient, maker_fee_cents


def _make_strategy(**config_overrides) -> tuple[MMStrategy, PaperOrderClient]:
    config = MMConfig(**config_overrides)
    strategy = MMStrategy(order_client=None, config=config)  # type: ignore[arg-type]
    client = PaperOrderClient(strategy)
    strategy._client = client
    return strategy, client


def _book_update(ticker: str, bid: int, ask: int, t: float = 1.0) -> OrderBookUpdate:
    return OrderBookUpdate(
        t_receipt=t, market_ticker=ticker,
        bid_yes=bid, ask_yes=ask,
        bid_size=10000, ask_size=10000,
    )


def _trade(ticker: str, price: int, side: str, size: int = 1, t: float = 2.0) -> TradeEvent:
    return TradeEvent(t_receipt=t, market_ticker=ticker, side=side, price=price, size=size)


class TestQuotingDecision:
    def test_quotes_on_wide_spread(self):
        s, c = _make_strategy(min_spread_cents=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        # Should have placed bid and ask
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 2
        sides = {o["action"] for o in places}
        assert sides == {"place_bid", "place_ask"}

    def test_no_quote_on_narrow_spread(self):
        s, c = _make_strategy(min_spread_cents=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 49, 50))  # spread = 1
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 0

    def test_ignores_non_kxnbapts(self):
        s, c = _make_strategy()
        s.on_event(_book_update("KXNBAGAME-TEST", 45, 50))
        assert len(c.order_log) == 0

    def test_no_quote_on_zero_spread(self):
        s, c = _make_strategy()
        s.on_event(_book_update("KXNBAPTS-TEST", 50, 50))
        assert len(c.order_log) == 0


class TestStateMachine:
    def test_no_double_order_when_pending(self):
        s, c = _make_strategy(min_spread_cents=3)
        # First update — places orders
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=1.0))
        n_after_first = len(c.order_log)
        # Second update at same prices — should NOT place new orders (already resting)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=2.0))
        assert len(c.order_log) == n_after_first

    def test_price_change_triggers_cancel_and_repost(self):
        s, c = _make_strategy(min_spread_cents=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=1.0))
        initial_count = len(c.order_log)
        # Price change
        s.on_event(_book_update("KXNBAPTS-TEST", 44, 50, t=2.0))
        # Should see cancel for old bid (price changed) but ask unchanged
        cancels = [o for o in c.order_log[initial_count:] if o["action"] == "cancel"]
        assert len(cancels) >= 1

    def test_cancel_ack_returns_to_idle(self):
        s, c = _make_strategy(min_spread_cents=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        bid_state = s._get_side("KXNBAPTS-TEST", "bid")
        assert bid_state.state == "resting"
        # Spread narrows — triggers cancel
        s.on_event(_book_update("KXNBAPTS-TEST", 49, 50))
        assert bid_state.state == "idle"  # cancel_ack delivered synchronously


class TestPositionLimits:
    def test_position_limit_stops_one_side(self):
        s, c = _make_strategy(min_spread_cents=3, max_position=2)
        s._positions["KXNBAPTS-TEST"] = 2  # at limit
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        # Should only place ask (can't buy more)
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 1
        assert places[0]["action"] == "place_ask"

    def test_negative_position_limit_stops_ask(self):
        s, c = _make_strategy(min_spread_cents=3, max_position=2)
        s._positions["KXNBAPTS-TEST"] = -2  # at short limit
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 1
        assert places[0]["action"] == "place_bid"

    def test_aggregate_limit(self):
        s, c = _make_strategy(min_spread_cents=3, max_aggregate_position=5)
        s._positions["KXNBAPTS-A"] = 3
        s._positions["KXNBAPTS-B"] = 2
        s._aggregate_abs_position = 5  # at aggregate limit
        s.on_event(_book_update("KXNBAPTS-C", 45, 50))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 0


class TestSkewing:
    def test_skew_when_long(self):
        s, c = _make_strategy(min_spread_cents=3, skew_threshold=2)
        s._positions["KXNBAPTS-TEST"] = 3  # above threshold
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        bid_place = [o for o in c.order_log if o["action"] == "place_bid"]
        assert len(bid_place) == 1
        assert bid_place[0]["price"] == 44  # widened by 1

    def test_skew_when_short(self):
        s, c = _make_strategy(min_spread_cents=3, skew_threshold=2)
        s._positions["KXNBAPTS-TEST"] = -3  # below threshold
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        ask_place = [o for o in c.order_log if o["action"] == "place_ask"]
        assert len(ask_place) == 1
        assert ask_place[0]["price"] == 51  # widened by 1


class TestFills:
    def test_fill_updates_position(self):
        s, c = _make_strategy(min_spread_cents=3, order_size=1)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        assert s._positions.get("KXNBAPTS-TEST", 0) == 0
        # Trade hits our bid (taker_side="no" at our bid price)
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no"))
        assert s._positions["KXNBAPTS-TEST"] == 1  # bought 1

    def test_fill_emits_event(self):
        s, c = _make_strategy(min_spread_cents=3, order_size=1)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no"))
        fill_events = [e for e in s.pending_events if isinstance(e, MMFillEvent)]
        assert len(fill_events) == 1
        assert fill_events[0].side == "buy"
        assert fill_events[0].position_after == 1

    def test_sell_fill(self):
        s, c = _make_strategy(min_spread_cents=3, order_size=1)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        # Trade hits our ask (taker_side="yes" at our ask price)
        s.on_event(_trade("KXNBAPTS-TEST", 50, "yes"))
        assert s._positions["KXNBAPTS-TEST"] == -1  # sold 1

    def test_no_fill_at_wrong_price(self):
        s, c = _make_strategy(min_spread_cents=3, order_size=1)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        # Trade at a different price
        s.on_event(_trade("KXNBAPTS-TEST", 47, "no"))
        assert s._positions.get("KXNBAPTS-TEST", 0) == 0


class TestBookInvalidated:
    def test_invalidation_cancels_orders(self):
        s, c = _make_strategy(min_spread_cents=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        # Both sides should be resting
        assert s._get_side("KXNBAPTS-TEST", "bid").state == "resting"
        assert s._get_side("KXNBAPTS-TEST", "ask").state == "resting"
        # Invalidate
        s.on_event(BookInvalidated(t_receipt=3.0, market_ticker="KXNBAPTS-TEST"))
        assert s._get_side("KXNBAPTS-TEST", "bid").state == "idle"
        assert s._get_side("KXNBAPTS-TEST", "ask").state == "idle"


class TestQuoteEvents:
    def test_emits_quote_on_change(self):
        s, c = _make_strategy(min_spread_cents=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=1.0))
        quotes = [e for e in s.pending_events if isinstance(e, MMQuoteEvent)]
        assert len(quotes) == 1
        assert quotes[0].bid_price == 45
        assert quotes[0].ask_price == 50

    def test_no_duplicate_quote_on_same_prices(self):
        s, c = _make_strategy(min_spread_cents=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=1.0))
        n = len([e for e in s.pending_events if isinstance(e, MMQuoteEvent)])
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=2.0))
        n2 = len([e for e in s.pending_events if isinstance(e, MMQuoteEvent)])
        assert n2 == n  # no new quote event


class TestMakerFee:
    def test_fee_at_midrange(self):
        # price=50: 0.0175 * 50 * 50 / 100 = 0.4375 → 0 → clamped to 1
        # Actually: 0.0175 * 50 * 50 / 100 = 43.75 / 100 = 0.4375
        # Wait: maker_fee_cents(50) = max(1, int(0.0175 * 50 * 50 / 100))
        # = max(1, int(43.75)) = max(1, 43) ... no.
        # 0.0175 * 50 * (100-50) / 100 = 0.0175 * 50 * 50 / 100 = 43.75
        # int(43.75) = 43? No: 0.0175 * 50 * 50 = 43.75, / 100 = 0.4375
        # int(0.4375) = 0, max(1, 0) = 1
        # Hmm, let me recompute. price_cents=50:
        # 0.0175 * 50 * (100-50) / 100 = 0.0175 * 50 * 50 / 100 = 0.0175 * 2500 / 100 = 43.75 / 100 = 0.4375
        # int(0.4375) = 0, max(1,0) = 1
        assert maker_fee_cents(50) == 1

    def test_fee_at_extreme_low(self):
        # price=1: 0.0175 * 1 * 99 / 100 = 1.7325 / 100 = 0.017325 → int=0 → 1
        assert maker_fee_cents(1) == 1

    def test_fee_at_extreme_high(self):
        # price=99: same as price=1 (symmetric)
        assert maker_fee_cents(99) == 1

    def test_fee_at_25(self):
        # price=25: 0.0175 * 25 * 75 / 100 = 32.8125 / 100 = 0.328125 → 0 → 1
        assert maker_fee_cents(25) == 1

    def test_fee_symmetry(self):
        # fee(p) == fee(100-p) by the C*(1-C) formula
        for p in range(1, 100):
            assert maker_fee_cents(p) == maker_fee_cents(100 - p)


class TestBookInvalidatedPending:
    def test_pending_state_resets_to_idle_without_cancel(self):
        """When a book is invalidated while an order is pending (no ACK yet),
        we should reset to idle directly — not try to cancel a non-existent order."""
        s, c = _make_strategy(min_spread_cents=3)
        # Manually set bid to pending (simulating: place sent, ACK not yet received)
        bid_state = s._get_side("KXNBAPTS-TEST", "bid")
        bid_state.state = "pending"
        bid_state.order_id = None  # no ACK yet
        bid_state.price = 45

        s.on_event(BookInvalidated(t_receipt=2.0, market_ticker="KXNBAPTS-TEST"))

        assert bid_state.state == "idle"
        assert bid_state.order_id is None
        assert bid_state.price is None
        # No cancel should have been issued for the pending order
        cancels = [o for o in c.order_log if o["action"] == "cancel"]
        assert len(cancels) == 0
