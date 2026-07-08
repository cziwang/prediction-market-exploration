"""Dual-view as-of join core (DESIGN.md D1 + The Two-Timestamp Problem).

Pure Python, no Flink imports: this class is the join *logic*, driven by
plain method calls. The Flink operator (pm.enrich.job) is a thin wrapper
that persists this state in Flink's keyed state and calls advance_watermark
from event-time timers. Keeping the logic pure makes it unit-testable in
milliseconds and guarantees the semantics are identical wherever it runs.

Semantics
─────────
One joiner instance per key (game_id). Both streams are buffered; nothing is
final until the watermark passes it:

    on_game_state(gs)         buffer
    on_trade(tagged_trade)    buffer
    advance_watermark(W)      process ALL buffered records with
                              t_receipt_ns <= W in timestamp order:
                                game state -> update the two views
                                trade      -> snapshot views, emit enriched

Why buffer game states too (not just trades): if states S1(t=1), S5(t=5)
both arrive before the watermark passes a trade T3(t=3), a "keep latest
only" design would enrich T3 with S5 — future information. Processing in
timestamp order enriches T3 with S1, exactly what a live system knew at t=3.

Determinism: output depends only on record timestamps and watermark
positions, never on arrival order. Ties at the same t_receipt_ns process
game states before trades (state "as of t" includes updates at t), then by
arrival sequence within each kind.

Views:
    receipt view — latest game state by t_receipt_ns (what our system knew)
    event view   — game state with max t_event_ns among those received
                   (what had actually happened on court, per best knowledge)
"""

import heapq
from dataclasses import dataclass, field

from pm.enrich.events import EnrichedTrade
from pm.enrich.features import edge, market_implied_probability, win_probability
from pm.kalshi.events import TradeEvent
from pm.nba.events import GameStateEvent

_GAME_STATE_PRIORITY = 0
_TRADE_PRIORITY = 1


@dataclass(frozen=True)
class TaggedTrade:
    """A TradeEvent routed to a game, with ticker semantics resolved upstream."""

    trade: TradeEvent
    game_id: str
    series: str
    yes_is_home: bool | None  # None => model probability not applicable


def _model_prob_for_side(home_prob: float, yes_is_home: bool | None) -> float | None:
    if yes_is_home is None:
        return None
    return home_prob if yes_is_home else 1.0 - home_prob


@dataclass
class AsOfJoiner:
    """Per-key (game_id) dual-view as-of join state machine."""

    # Buffered records: (t_receipt_ns, priority, seq, payload)
    _buffer: list[tuple[int, int, int, object]] = field(default_factory=list)
    _seq: int = 0

    # Receipt view: latest processed game state by t_receipt_ns
    _receipt_state: GameStateEvent | None = None
    # Event view: processed game state with max t_event_ns
    _event_state: GameStateEvent | None = None

    @classmethod
    def restore(
        cls,
        receipt_state: GameStateEvent | None,
        event_state: GameStateEvent | None,
    ) -> "AsOfJoiner":
        """Rebuild a joiner from externally persisted view states (used by the
        Flink operator, which stores views and pending records granularly)."""
        joiner = cls()
        joiner._receipt_state = receipt_state
        joiner._event_state = event_state
        return joiner

    @property
    def views(self) -> tuple[GameStateEvent | None, GameStateEvent | None]:
        """(receipt_view, event_view) — for external persistence."""
        return self._receipt_state, self._event_state

    @property
    def pending(self) -> list[TaggedTrade | GameStateEvent]:
        """Buffered records not yet finalized, in buffer (timestamp) order."""
        return [payload for _, _, _, payload in sorted(self._buffer)]  # type: ignore[misc]

    def on_game_state(self, gs: GameStateEvent) -> None:
        heapq.heappush(self._buffer, (gs.t_receipt_ns, _GAME_STATE_PRIORITY, self._seq, gs))
        self._seq += 1

    def on_trade(self, tagged: TaggedTrade) -> None:
        heapq.heappush(
            self._buffer, (tagged.trade.t_receipt_ns, _TRADE_PRIORITY, self._seq, tagged)
        )
        self._seq += 1

    def advance_watermark(self, watermark_ns: int) -> list[EnrichedTrade]:
        """Finalize all buffered records with t_receipt_ns <= watermark_ns."""
        out: list[EnrichedTrade] = []
        while self._buffer and self._buffer[0][0] <= watermark_ns:
            _, priority, _, payload = heapq.heappop(self._buffer)
            if priority == _GAME_STATE_PRIORITY:
                self._apply_game_state(payload)  # type: ignore[arg-type]
            else:
                out.append(self._enrich(payload))  # type: ignore[arg-type]
        return out

    def _apply_game_state(self, gs: GameStateEvent) -> None:
        # Receipt view: buffer pops in t_receipt order, so this is always newest
        self._receipt_state = gs
        # Event view: keep the max t_event_ns seen (CDN can deliver out of
        # on-court order across polls; None t_event_ns never wins)
        if gs.t_event_ns is not None and (
            self._event_state is None
            or self._event_state.t_event_ns is None
            or gs.t_event_ns > self._event_state.t_event_ns
        ):
            self._event_state = gs

    def _enrich(self, tagged: TaggedTrade) -> EnrichedTrade:
        trade = tagged.trade
        implied = market_implied_probability(trade.price_cents)

        base: dict[str, object] = dict(
            t_trade_receipt_ns=trade.t_receipt_ns,
            t_trade_exchange_ns=trade.t_exchange_ns,
            market_ticker=trade.market_ticker,
            series=tagged.series,
            trade_price=trade.price_cents,
            trade_size_cc=trade.size_cc,
            taker_side=trade.taker_side,
            game_id=tagged.game_id,
            market_implied_prob=implied,
        )

        rs = self._receipt_state
        if rs is not None:
            r_home_prob = win_probability(rs.score_diff, rs.seconds_remaining)
            r_model = _model_prob_for_side(r_home_prob, tagged.yes_is_home)
            base.update(
                r_period=rs.period,
                r_seconds_remaining=rs.seconds_remaining,
                r_score_diff=rs.score_diff,
                r_model_prob=r_model,
                r_edge=None if r_model is None else edge(r_model, implied),
                r_info_delay_ms=(trade.t_receipt_ns - rs.t_receipt_ns) // 1_000_000,
            )

        es = self._event_state
        if es is not None:
            e_home_prob = win_probability(es.score_diff, es.seconds_remaining)
            e_model = _model_prob_for_side(e_home_prob, tagged.yes_is_home)
            e_delay = None
            if trade.t_exchange_ns is not None and es.t_event_ns is not None:
                e_delay = (trade.t_exchange_ns - es.t_event_ns) // 1_000_000
            base.update(
                e_period=es.period,
                e_seconds_remaining=es.seconds_remaining,
                e_score_diff=es.score_diff,
                e_model_prob=e_model,
                e_edge=None if e_model is None else edge(e_model, implied),
                e_info_delay_ms=e_delay,
            )

        return EnrichedTrade(**base)  # type: ignore[arg-type]
