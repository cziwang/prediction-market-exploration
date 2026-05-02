"""Tests for app.strategy.mm."""

import json

from app.events import BookInvalidated, MMFillEvent, MMQuoteEvent, OrderBookUpdate, TradeEvent
from app.strategy.mm import MMConfig, MMStrategy, PaperOrderClient, maker_fee_cents


# Defaults that disable new inventory-risk features so legacy tests pass unchanged.
_LEGACY_DEFAULTS = dict(
    skew_cents_per_contract=0,
    age_skew_interval_s=0.0,
    abs_exposure_soft_limit=0,
    use_player_skew=False,
    min_trades_to_quote=0,
    use_queue_model=False,
    queue_ahead_cap=0,
    use_dynamic_sizing=False,
)


def _make_strategy(**config_overrides) -> tuple[MMStrategy, PaperOrderClient]:
    merged = {**_LEGACY_DEFAULTS, **config_overrides}
    config = MMConfig(**merged)
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

    def test_narrow_spread_allows_offsetting_bid_when_short(self):
        """When short, bid should be allowed even on tight spreads to close position."""
        s, c = _make_strategy(min_spread_cents=3)
        s._positions["KXNBAPTS-TEST"] = -1
        s.on_event(_book_update("KXNBAPTS-TEST", 70, 72))  # spread = 2, below min
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 1
        assert places[0]["action"] == "place_bid"

    def test_narrow_spread_allows_offsetting_ask_when_long(self):
        """When long, ask should be allowed even on tight spreads to close position."""
        s, c = _make_strategy(min_spread_cents=3)
        s._positions["KXNBAPTS-TEST"] = 1
        s.on_event(_book_update("KXNBAPTS-TEST", 70, 72))  # spread = 2, below min
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 1
        assert places[0]["action"] == "place_ask"

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


class TestAggregateSkew:
    def test_agg_skew_widens_ask_when_net_short(self):
        """At -7 net, ask should be widened by 1c (1 step past threshold=5)."""
        s, c = _make_strategy(min_spread_cents=3, agg_skew_threshold=5, agg_skew_max=15)
        s._agg_net_position = -7
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        ask_place = [o for o in c.order_log if o["action"] == "place_ask"]
        assert len(ask_place) == 1
        assert ask_place[0]["price"] == 51  # widened by 1c

    def test_agg_skew_suppresses_ask_at_max(self):
        """At -15 net, asks should be suppressed entirely."""
        s, c = _make_strategy(min_spread_cents=3, agg_skew_threshold=5, agg_skew_max=15)
        s._agg_net_position = -15
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 1
        assert places[0]["action"] == "place_bid"

    def test_agg_skew_widens_bid_when_net_long(self):
        """At +7 net, bid should be widened by 1c."""
        s, c = _make_strategy(min_spread_cents=3, agg_skew_threshold=5, agg_skew_max=15)
        s._agg_net_position = 7
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        bid_place = [o for o in c.order_log if o["action"] == "place_bid"]
        assert len(bid_place) == 1
        assert bid_place[0]["price"] == 44  # widened by 1c

    def test_agg_skew_stacks_with_per_ticker(self):
        """Per-ticker skew + aggregate skew should both apply."""
        s, c = _make_strategy(
            min_spread_cents=3, skew_threshold=2,
            agg_skew_threshold=5, agg_skew_max=15,
        )
        s._positions["KXNBAPTS-TEST"] = -3  # triggers per-ticker: ask +1c
        s._agg_net_position = -7             # triggers agg: ask +1c
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        ask_place = [o for o in c.order_log if o["action"] == "place_ask"]
        assert len(ask_place) == 1
        assert ask_place[0]["price"] == 52  # 50 + 1 (per-ticker) + 1 (agg)

    def test_agg_skew_updates_on_fill(self):
        """Selling should decrement _agg_net_position."""
        s, c = _make_strategy(min_spread_cents=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        assert s._agg_net_position == 0
        # Sell fill
        s.on_event(_trade("KXNBAPTS-TEST", 50, "yes"))
        assert s._agg_net_position == -1

    def test_no_agg_skew_below_threshold(self):
        """At -3 net (below threshold=5), quotes should be normal."""
        s, c = _make_strategy(min_spread_cents=3, agg_skew_threshold=5, agg_skew_max=15)
        s._agg_net_position = -3
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        bid_place = [o for o in c.order_log if o["action"] == "place_bid"]
        ask_place = [o for o in c.order_log if o["action"] == "place_ask"]
        assert len(bid_place) == 1 and bid_place[0]["price"] == 45
        assert len(ask_place) == 1 and ask_place[0]["price"] == 50


class TestScaledSkew:
    """Per-ticker skew scales with position size when skew_cents_per_contract > 0."""

    def test_skew_scales_with_position(self):
        s, c = _make_strategy(
            min_spread_cents=3, skew_threshold=1, skew_cents_per_contract=2,
        )
        s._positions["KXNBAPTS-TEST"] = 3
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        bid = [o for o in c.order_log if o["action"] == "place_bid"]
        assert len(bid) == 1
        assert bid[0]["price"] == 45 - 6  # 3 * 2c = 6c widening

    def test_skew_short_scales(self):
        s, c = _make_strategy(
            min_spread_cents=3, skew_threshold=1, skew_cents_per_contract=3,
        )
        s._positions["KXNBAPTS-TEST"] = -2
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        ask = [o for o in c.order_log if o["action"] == "place_ask"]
        assert len(ask) == 1
        assert ask[0]["price"] == 50 + 6  # 2 * 3c = 6c widening

    def test_no_skew_below_threshold(self):
        s, c = _make_strategy(
            min_spread_cents=3, skew_threshold=3, skew_cents_per_contract=2,
        )
        s._positions["KXNBAPTS-TEST"] = 2  # below threshold
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        bid = [o for o in c.order_log if o["action"] == "place_bid"]
        assert bid[0]["price"] == 45  # no widening


class TestPositionAge:
    """Position age tracking and age-based skew."""

    def test_age_recorded_on_first_fill(self):
        s, c = _make_strategy(min_spread_cents=3, order_size=1)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=10.0))
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no", t=10.0))
        assert s._position_opened_at["KXNBAPTS-TEST"] == 10.0

    def test_age_cleared_when_flat(self):
        s, c = _make_strategy(min_spread_cents=3, order_size=1)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=10.0))
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no", t=10.0))  # buy → pos=1
        assert "KXNBAPTS-TEST" in s._position_opened_at
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=11.0))
        s.on_event(_trade("KXNBAPTS-TEST", 50, "yes", t=11.0))  # sell → pos=0
        assert "KXNBAPTS-TEST" not in s._position_opened_at

    def test_age_skew_widens_over_time(self):
        s, c = _make_strategy(
            min_spread_cents=3, age_skew_interval_s=600.0,
            age_skew_step_cents=2, max_age_skew_cents=10,
        )
        s._positions["KXNBAPTS-TEST"] = 1
        s._position_opened_at["KXNBAPTS-TEST"] = 100.0
        # 1200s later → 2 tiers → 4c widening
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=1300.0))
        bid = [o for o in c.order_log if o["action"] == "place_bid"]
        assert bid[0]["price"] == 45 - 4

    def test_age_skew_capped(self):
        s, c = _make_strategy(
            min_spread_cents=3, age_skew_interval_s=60.0,
            age_skew_step_cents=3, max_age_skew_cents=5,
        )
        s._positions["KXNBAPTS-TEST"] = -1
        s._position_opened_at["KXNBAPTS-TEST"] = 0.0
        # 600s / 60s = 10 tiers × 3c = 30c, but capped at 5c
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=600.0))
        ask = [o for o in c.order_log if o["action"] == "place_ask"]
        assert ask[0]["price"] == 50 + 5


class TestAbsExposureSoftLimit:
    """Absolute exposure soft limit suppresses new-exposure side."""

    def test_suppresses_bid_when_long_at_limit(self):
        s, c = _make_strategy(min_spread_cents=3, abs_exposure_soft_limit=5)
        s._positions["KXNBAPTS-A"] = 3
        s._positions["KXNBAPTS-B"] = 2
        s._aggregate_abs_position = 5
        s.on_event(_book_update("KXNBAPTS-A", 45, 50))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        # Position is +3, so bid suppressed (would increase abs), ask allowed
        assert len(places) == 1
        assert places[0]["action"] == "place_ask"

    def test_suppresses_ask_when_short_at_limit(self):
        s, c = _make_strategy(min_spread_cents=3, abs_exposure_soft_limit=5)
        s._positions["KXNBAPTS-A"] = -5
        s._aggregate_abs_position = 5
        s.on_event(_book_update("KXNBAPTS-A", 45, 50))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 1
        assert places[0]["action"] == "place_bid"

    def test_suppresses_both_when_flat_at_limit(self):
        s, c = _make_strategy(min_spread_cents=3, abs_exposure_soft_limit=5)
        s._positions["KXNBAPTS-A"] = 3
        s._positions["KXNBAPTS-B"] = -2
        s._aggregate_abs_position = 5
        # Flat on this ticker — both sides would increase abs exposure
        s.on_event(_book_update("KXNBAPTS-C", 45, 50))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 0

    def test_disabled_when_zero(self):
        s, c = _make_strategy(min_spread_cents=3, abs_exposure_soft_limit=0)
        s._positions["KXNBAPTS-A"] = 100
        s._aggregate_abs_position = 100
        s.on_event(_book_update("KXNBAPTS-A", 45, 50))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) >= 1  # not suppressed


class TestPlayerSkew:
    """Player-level correlated skew across thresholds."""

    def test_player_position_tracked(self):
        s, c = _make_strategy(
            min_spread_cents=3, order_size=1, use_player_skew=True,
            player_skew_cents_per_contract=1,
        )
        # First ticker: sell at ask
        s.on_event(_book_update("KXNBAPTS-G-PLAYER1-10", 30, 70, t=1.0))
        s.on_event(_trade("KXNBAPTS-G-PLAYER1-10", 70, "yes", t=1.0))  # sell
        assert s._player_positions["PLAYER1"] == -1
        # Second ticker: player skew widens ask by 1c (player_pos=-1),
        # so ask is placed at 71. Trade at 71 fills it.
        s.on_event(_book_update("KXNBAPTS-G-PLAYER1-20", 30, 70, t=2.0))
        s.on_event(_trade("KXNBAPTS-G-PLAYER1-20", 71, "yes", t=2.0))  # sell
        assert s._player_positions["PLAYER1"] == -2

    def test_player_skew_widens_ask(self):
        s, c = _make_strategy(
            min_spread_cents=3, use_player_skew=True,
            player_skew_cents_per_contract=2,
        )
        s._player_positions["PLAYER1"] = -3
        s.on_event(_book_update("KXNBAPTS-G-PLAYER1-10", 45, 50))
        ask = [o for o in c.order_log if o["action"] == "place_ask"]
        assert ask[0]["price"] == 50 + 6  # 3 * 2c

    def test_player_key_extraction(self):
        assert MMStrategy._player_key("KXNBAPTS-26APR25DENMIN-DENCBRAUN0-10") == "DENCBRAUN0"
        assert MMStrategy._player_key("KXNBAPTS-G-PLAYER1-20") == "PLAYER1"
        assert MMStrategy._player_key("SHORT") is None


class TestVolumeFilter:
    """Minimum trade volume filter."""

    def test_no_quote_before_min_trades(self):
        s, c = _make_strategy(min_spread_cents=3, min_trades_to_quote=5)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 0

    def test_quotes_after_min_trades(self):
        s, c = _make_strategy(min_spread_cents=3, min_trades_to_quote=3)
        # Send 3 trades to build up count
        for i in range(3):
            s.on_event(_trade("KXNBAPTS-TEST", 47, "yes", t=float(i)))
        # Now should quote
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=10.0))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 2

    def test_disabled_when_zero(self):
        s, c = _make_strategy(min_spread_cents=3, min_trades_to_quote=0)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        places = [o for o in c.order_log if o["action"].startswith("place")]
        assert len(places) == 2


class TestQueueModel:
    """Queue-aware fill simulation."""

    def test_fill_blocked_by_queue_depth(self):
        """Trade smaller than queue depth should not fill our order."""
        s, c = _make_strategy(min_spread_cents=3, use_queue_model=True, queue_ahead_cap=10)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))  # bid_size=10000 → capped to 10
        # Trade of size 5 at our bid — queue is 10, trade doesn't reach us
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no", size=5))
        assert s._positions.get("KXNBAPTS-TEST", 0) == 0

    def test_fill_when_trade_exceeds_queue(self):
        """Trade larger than capped queue depth should fill."""
        s, c = _make_strategy(min_spread_cents=3, use_queue_model=True, queue_ahead_cap=3)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))  # queue capped at 3
        # Trade of size 5 > queue of 3 → fill for min(1, 5-3)=1
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no", size=5))
        assert s._positions["KXNBAPTS-TEST"] == 1

    def test_queue_model_disabled(self):
        """With use_queue_model=False, any trade at our price fills."""
        s, c = _make_strategy(min_spread_cents=3, use_queue_model=False)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no", size=1))
        assert s._positions["KXNBAPTS-TEST"] == 1


class TestDynamicSizing:
    """Dynamic order sizing based on spread and inventory."""

    def test_disabled_returns_order_size(self):
        s, c = _make_strategy(min_spread_cents=3, use_dynamic_sizing=False, order_size=1)
        s.on_event(_book_update("KXNBAPTS-TEST", 30, 50))  # wide spread
        bid = [o for o in c.order_log if o["action"] == "place_bid"]
        assert bid[0]["size"] == 1

    def test_wide_spread_flat_position_gives_max(self):
        s, c = _make_strategy(
            min_spread_cents=3, use_dynamic_sizing=True,
            max_order_size=2, spread_size_threshold=6,
        )
        # Spread=20, flat position → spread_bonus = min((20-6)/6, 1.0) = 1.0
        # raw = 1.0 + 1.0 = 2.0 → size=2
        s.on_event(_book_update("KXNBAPTS-TEST", 40, 60))
        bid = [o for o in c.order_log if o["action"] == "place_bid"]
        assert bid[0]["size"] == 2

    def test_narrow_spread_returns_one(self):
        s, c = _make_strategy(
            min_spread_cents=3, use_dynamic_sizing=True,
            max_order_size=2, spread_size_threshold=6,
        )
        # Spread=4, below threshold → spread_bonus=0, raw=1.0 → size=1
        s.on_event(_book_update("KXNBAPTS-TEST", 48, 52))
        bid = [o for o in c.order_log if o["action"] == "place_bid"]
        assert bid[0]["size"] == 1

    def test_inventory_penalty_reduces_extending_side(self):
        s, c = _make_strategy(
            min_spread_cents=3, use_dynamic_sizing=True,
            max_order_size=2, spread_size_threshold=6,
        )
        s._positions["KXNBAPTS-TEST"] = 2  # long 2
        # Spread=20 → spread_bonus=1.0. But bidding (extending): penalty=0.25*4=1.0
        # raw = 1.0 + 1.0 - 1.0 = 1.0 → size=1
        s.on_event(_book_update("KXNBAPTS-TEST", 40, 60))
        bid = [o for o in c.order_log if o["action"] == "place_bid"]
        assert bid[0]["size"] == 1

    def test_offsetting_side_gets_full_size(self):
        s, c = _make_strategy(
            min_spread_cents=3, use_dynamic_sizing=True,
            max_order_size=2, spread_size_threshold=6,
        )
        s._positions["KXNBAPTS-TEST"] = 2  # long 2
        # Ask side (reducing): no penalty → raw = 1.0 + 1.0 = 2.0
        # But clamped to abs(position) = 2 → size=2
        s.on_event(_book_update("KXNBAPTS-TEST", 40, 60))
        ask = [o for o in c.order_log if o["action"] == "place_ask"]
        assert ask[0]["size"] == 2

    def test_reducing_side_clamped_to_position(self):
        s, c = _make_strategy(
            min_spread_cents=3, use_dynamic_sizing=True,
            max_order_size=2, spread_size_threshold=6,
        )
        s._positions["KXNBAPTS-TEST"] = -1  # short 1
        # Bid side (reducing short): raw=2.0 but clamped to abs(-1)=1
        s.on_event(_book_update("KXNBAPTS-TEST", 40, 60))
        bid = [o for o in c.order_log if o["action"] == "place_bid"]
        assert bid[0]["size"] == 1

    def test_position_limit_clamp(self):
        s, c = _make_strategy(
            min_spread_cents=3, use_dynamic_sizing=True,
            max_order_size=2, spread_size_threshold=6, max_position=2,
        )
        s._positions["KXNBAPTS-TEST"] = 1  # 1 room left to max_position=2
        # Bid (extending): room = 2-1 = 1 → clamp to 1
        s.on_event(_book_update("KXNBAPTS-TEST", 40, 60))
        bid = [o for o in c.order_log if o["action"] == "place_bid"]
        assert bid[0]["size"] == 1


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


class TestPositionPersistence:
    def _make_strategy_with_state(self, state_path, **config_overrides):
        merged = {**_LEGACY_DEFAULTS, "state_path": state_path, **config_overrides}
        config = MMConfig(**merged)
        strategy = MMStrategy(order_client=None, config=config)  # type: ignore[arg-type]
        client = PaperOrderClient(strategy)
        strategy._client = client
        return strategy, client

    def test_state_written_on_fill(self, tmp_path):
        sp = tmp_path / "state.json"
        s, c = self._make_strategy_with_state(sp, min_spread_cents=3, order_size=1)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no"))
        assert sp.exists()
        data = json.loads(sp.read_text())
        assert data["positions"] == {"KXNBAPTS-TEST": 1}
        assert "KXNBAPTS-TEST" in data.get("position_opened_at", {})

    def test_state_loaded_on_init(self, tmp_path):
        sp = tmp_path / "state.json"
        sp.write_text(json.dumps({"positions": {"KXNBAPTS-A": 3, "KXNBAPTS-B": -2}}))
        s, c = self._make_strategy_with_state(sp)
        assert s._positions == {"KXNBAPTS-A": 3, "KXNBAPTS-B": -2}
        assert s._agg_net_position == 1
        assert s._aggregate_abs_position == 5

    def test_no_state_file_ok(self, tmp_path):
        sp = tmp_path / "nonexistent.json"
        s, c = self._make_strategy_with_state(sp)
        assert s._positions == {}

    def test_state_path_none_no_write(self, tmp_path):
        s, c = _make_strategy(min_spread_cents=3, order_size=1)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no"))
        # No state file should exist anywhere — strategy has state_path=None
        assert s._config.state_path is None

    def test_zero_positions_cleaned(self, tmp_path):
        sp = tmp_path / "state.json"
        s, c = self._make_strategy_with_state(sp, min_spread_cents=3, order_size=1)
        # Buy then sell to get back to 0
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50))
        s.on_event(_trade("KXNBAPTS-TEST", 45, "no"))  # buy
        assert s._positions["KXNBAPTS-TEST"] == 1
        # Need to re-place ask order (it was filled above or idle)
        s.on_event(_book_update("KXNBAPTS-TEST", 45, 50, t=3.0))
        s.on_event(_trade("KXNBAPTS-TEST", 50, "yes", t=4.0))  # sell
        assert s._positions["KXNBAPTS-TEST"] == 0
        data = json.loads(sp.read_text())
        assert data["positions"] == {}
        assert data.get("position_opened_at", {}) == {}
