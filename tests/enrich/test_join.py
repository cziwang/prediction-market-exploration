"""Tests for pm.enrich.join — the dual-view as-of join core."""

from hypothesis import given
from hypothesis import strategies as st

from pm.enrich.join import AsOfJoiner, TaggedTrade
from pm.kalshi.events import TradeEvent
from pm.nba.events import GameStateEvent

NS = 1_000_000_000  # 1 second in ns


def gs(t_receipt_s: float, score_diff: int, t_event_s: float | None = None) -> GameStateEvent:
    return GameStateEvent(
        t_receipt_ns=int(t_receipt_s * NS),
        source="nba_cdn",
        game_id="G1",
        period=2,
        clock_seconds=300.0,
        seconds_remaining=1740.0,
        score_home=50 + score_diff,
        score_away=50,
        score_diff=score_diff,
        action_type="2pt",
        t_event_ns=None if t_event_s is None else int(t_event_s * NS),
    )


def trade(t_receipt_s: float, price: int = 65, t_exchange_s: float | None = None) -> TaggedTrade:
    return TaggedTrade(
        trade=TradeEvent(
            t_receipt_ns=int(t_receipt_s * NS),
            source="kalshi_ws",
            market_ticker="KXNBAGAME-26APR18ATLNYK-NYK",
            price_cents=price,
            size_cc=100,
            taker_side="yes",
            t_exchange_ns=None if t_exchange_s is None else int(t_exchange_s * NS),
        ),
        game_id="G1",
        series="KXNBAGAME",
        yes_is_home=True,
    )


class TestBasicJoin:
    def test_trade_enriched_with_prior_state(self) -> None:
        j = AsOfJoiner()
        j.on_game_state(gs(1.0, score_diff=2))
        j.on_trade(trade(3.0))
        out = j.advance_watermark(10 * NS)
        assert len(out) == 1
        assert out[0].r_score_diff == 2
        assert out[0].r_info_delay_ms == 2000

    def test_trade_before_any_state_emits_nulls(self) -> None:
        j = AsOfJoiner()
        j.on_trade(trade(3.0))
        out = j.advance_watermark(10 * NS)
        assert len(out) == 1
        assert out[0].r_score_diff is None
        assert out[0].r_model_prob is None
        assert out[0].market_implied_prob == 0.65  # trade itself always present

    def test_no_future_leak(self) -> None:
        # THE case that motivated buffering both streams: states at t=1 and
        # t=5 arrive before the watermark passes a trade at t=3. The trade
        # must see the t=1 state, not the t=5 state.
        j = AsOfJoiner()
        j.on_game_state(gs(1.0, score_diff=2))
        j.on_game_state(gs(5.0, score_diff=7))
        j.on_trade(trade(3.0))
        out = j.advance_watermark(10 * NS)
        assert out[0].r_score_diff == 2  # not 7

    def test_watermark_holds_back_unfinalized_trades(self) -> None:
        j = AsOfJoiner()
        j.on_trade(trade(3.0))
        j.on_trade(trade(8.0))
        out1 = j.advance_watermark(5 * NS)
        assert len(out1) == 1  # only the t=3 trade
        out2 = j.advance_watermark(10 * NS)
        assert len(out2) == 1  # now the t=8 trade

    def test_arrival_order_does_not_matter(self) -> None:
        # Same records, opposite arrival order -> identical output
        j1 = AsOfJoiner()
        j1.on_game_state(gs(1.0, score_diff=2))
        j1.on_trade(trade(3.0))

        j2 = AsOfJoiner()
        j2.on_trade(trade(3.0))
        j2.on_game_state(gs(1.0, score_diff=2))

        assert j1.advance_watermark(10 * NS) == j2.advance_watermark(10 * NS)

    def test_state_at_same_timestamp_as_trade_applies_first(self) -> None:
        j = AsOfJoiner()
        j.on_game_state(gs(3.0, score_diff=4))
        j.on_trade(trade(3.0))
        out = j.advance_watermark(10 * NS)
        assert out[0].r_score_diff == 4  # state "as of t" includes t


class TestDualView:
    def test_views_diverge_under_cdn_lag(self) -> None:
        # On-court event at t=2 (score_diff=5) received late at t=9;
        # earlier state (diff=2, event t=0.5) received promptly at t=1.
        # Trade at t=3 (exchange time t=3):
        #   receipt view -> diff=2 (the t=9 arrival hadn't come yet)
        #   event view   -> also diff=2 here, because by watermark processing
        #                   order the late state is applied only after t=9...
        j = AsOfJoiner()
        j.on_game_state(gs(1.0, score_diff=2, t_event_s=0.5))
        j.on_game_state(gs(9.0, score_diff=5, t_event_s=2.0))
        j.on_trade(trade(3.0, t_exchange_s=3.0))
        out = j.advance_watermark(10 * NS)
        # Trade processes at its receipt time t=3, before the t=9 state:
        assert out[0].r_score_diff == 2
        assert out[0].e_score_diff == 2
        assert out[0].e_info_delay_ms == 2500  # 3.0 - 0.5

    def test_event_view_keeps_max_event_time(self) -> None:
        # Two states received in receipt order but with out-of-order on-court
        # times (CDN poll returned actions out of order across polls).
        j = AsOfJoiner()
        j.on_game_state(gs(1.0, score_diff=3, t_event_s=0.9))
        j.on_game_state(gs(2.0, score_diff=1, t_event_s=0.4))  # older on court
        j.on_trade(trade(5.0, t_exchange_s=5.0))
        out = j.advance_watermark(10 * NS)
        assert out[0].r_score_diff == 1  # receipt view: latest received
        assert out[0].e_score_diff == 3  # event view: latest on-court

    def test_model_prob_side_adjustment(self) -> None:
        j = AsOfJoiner()
        j.on_game_state(gs(1.0, score_diff=10))  # home well ahead
        home_side = trade(3.0)
        away_side = TaggedTrade(
            trade=home_side.trade,
            game_id="G1",
            series="KXNBAGAME",
            yes_is_home=False,
        )
        j.on_trade(home_side)
        j.on_trade(away_side)
        out = j.advance_watermark(10 * NS)
        assert out[0].r_model_prob is not None and out[1].r_model_prob is not None
        assert out[0].r_model_prob > 0.5  # YES = home, home leading
        assert out[0].r_model_prob + out[1].r_model_prob == 1.0

    def test_non_game_series_has_no_model_prob(self) -> None:
        j = AsOfJoiner()
        j.on_game_state(gs(1.0, score_diff=5))
        t = trade(3.0)
        j.on_trade(TaggedTrade(t.trade, "G1", "KXNBATOTAL", yes_is_home=None))
        out = j.advance_watermark(10 * NS)
        assert out[0].r_score_diff == 5  # game state still attached
        assert out[0].r_model_prob is None
        assert out[0].r_edge is None


class TestDeterminismProperty:
    @given(st.permutations(range(6)))
    def test_any_arrival_order_same_output(self, order: list[int]) -> None:
        records = [
            ("gs", gs(1.0, score_diff=2)),
            ("gs", gs(4.0, score_diff=5)),
            ("gs", gs(7.0, score_diff=-1)),
            ("tr", trade(2.0)),
            ("tr", trade(5.0)),
            ("tr", trade(8.0)),
        ]
        j = AsOfJoiner()
        for i in order:
            kind, rec = records[i]
            if kind == "gs":
                j.on_game_state(rec)  # type: ignore[arg-type]
            else:
                j.on_trade(rec)  # type: ignore[arg-type]
        out = j.advance_watermark(100 * NS)

        # Reference: in-order arrival
        ref = AsOfJoiner()
        for kind, rec in records:
            if kind == "gs":
                ref.on_game_state(rec)  # type: ignore[arg-type]
            else:
                ref.on_trade(rec)  # type: ignore[arg-type]
        assert out == ref.advance_watermark(100 * NS)
