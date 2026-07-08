"""PyFlink enrichment job (DESIGN.md §3): the dual-view as-of join.

Topology:

    KafkaSource(kalshi.trades)   --normalize+route--> keyed by game_id --+
                                                                         +--> AsOfJoinOperator
    KafkaSource(nba.game_state)  --normalize-------->  keyed by game_id -+           |
                                                                                     v
                                                                        KafkaSink(enriched.trades)

The join semantics live in pm.enrich.join.AsOfJoiner (pure Python, fully
unit-tested). This module is only plumbing: Kafka sources with per-source
watermark strategies, normalization at the boundary, keying, the operator
shell that persists the joiner in Flink keyed state and drives it from
event-time timers, and the sink.

Current scope (first iteration):
- ticker -> game_id via a static event-code mapping (broadcast reference
  topic per DESIGN.md D4 is a later step)
- malformed/unparseable records go to the dlq.enrich Kafka topic (D8);
  markets for games outside the game_map are *skipped*, not DLQ'd — they are
  expected, not errors
- bounded mode: consume up to the topic's current end offsets, then finish —
  right for replay-driven development; live mode drops set_bounded()

Run:
    .venv/bin/python -m pm.enrich.job --game-map '{"26APR18ATLNYK": "0042500121"}'
"""

import argparse
import json
import logging
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

from pyflink.common import Duration, Types, WatermarkStrategy
from pyflink.common.serialization import SimpleStringSchema
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import (
    KafkaOffsetsInitializer,
    KafkaRecordSerializationSchema,
    KafkaSink,
    KafkaSource,
)
from pyflink.datastream.functions import KeyedCoProcessFunction, RuntimeContext
from pyflink.datastream.state import ValueStateDescriptor

from pm.core.dlq import Dlq, Ok
from pm.enrich.join import AsOfJoiner, TaggedTrade
from pm.kalshi.events import TradeEvent
from pm.kalshi.normalize import normalize_kalshi
from pm.kalshi.ticker import parse_nba_ticker
from pm.nba.events import GameStateEvent
from pm.nba.normalize import normalize_nba_pbp

log = logging.getLogger(__name__)

JARS_DIR = Path(__file__).parent.parent.parent.parent / "jars"
ENRICHED_TOPIC = "enriched.trades"
DLQ_TOPIC = "dlq.enrich"

# Classification of every input record (D8: nothing vanishes silently):
OK = "ok"  # normalized + routed -> join
DLQ = "dlq"  # data error -> dlq.enrich topic with raw + reason
SKIP = "skip"  # valid market, untracked game -> expected, dropped by design


# ─── Normalization / routing (raw Kafka string -> typed, game_id-keyed) ──────


def _dlq_record(source_topic: str, raw: str, error: str) -> str:
    return json.dumps({"source_topic": source_topic, "raw": raw, "error": error})


def classify_trade(raw: str, game_map: dict[str, str]) -> tuple[str, Any]:
    """Classify a Kalshi trade line: (OK, TaggedTrade) | (DLQ, json) | (SKIP, None)."""
    result = normalize_kalshi(raw.encode())
    if isinstance(result, Dlq):
        return DLQ, _dlq_record("kalshi.trades", raw, result.error)
    assert isinstance(result, Ok)
    assert isinstance(result.event, TradeEvent)
    trade = result.event
    parsed = parse_nba_ticker(trade.market_ticker)
    if parsed is None:
        return DLQ, _dlq_record("kalshi.trades", raw, f"unparseable ticker: {trade.market_ticker}")
    game_id = game_map.get(parsed.event_code)
    if game_id is None:
        return SKIP, None  # market for a game we're not tracking
    return OK, TaggedTrade(
        trade=trade,
        game_id=game_id,
        series=parsed.series,
        yes_is_home=parsed.yes_is_home,
    )


def classify_game_state(raw: str) -> tuple[str, Any]:
    """Classify a PBP line: (OK, GameStateEvent) | (DLQ, json)."""
    result = normalize_nba_pbp(raw.encode())
    if isinstance(result, Dlq):
        return DLQ, _dlq_record("nba.game_state", raw, result.error)
    assert isinstance(result, Ok)
    assert isinstance(result.event, GameStateEvent)
    return OK, result.event


# ─── The operator shell ───────────────────────────────────────────────────────


class AsOfJoinOperator(KeyedCoProcessFunction):  # type: ignore[misc]
    """Thin Flink shell around AsOfJoiner.

    Per key (game_id): the entire joiner (buffers + view states) is persisted
    as one pickled ValueState. Every buffered record registers an event-time
    timer at its receipt timestamp; when the watermark passes, on_timer
    finalizes everything up to that point via advance_watermark.

    Timer/watermark domain is Kafka's millisecond timestamps; the joiner works
    in nanoseconds. A timer firing at ms M means the watermark guarantees no
    more records in ms-bucket <= M, i.e. nothing earlier than (M+1)*1e6 ns.
    """

    def __init__(self) -> None:
        self._state: Any = None

    def open(self, runtime_context: RuntimeContext) -> None:
        self._state = runtime_context.get_state(
            ValueStateDescriptor("joiner", Types.PICKLED_BYTE_ARRAY())
        )

    def _joiner(self) -> AsOfJoiner:
        return self._state.value() or AsOfJoiner()

    def process_element1(self, tagged: TaggedTrade, ctx: Any) -> None:  # trades
        joiner = self._joiner()
        joiner.on_trade(tagged)
        self._state.update(joiner)
        ctx.timer_service().register_event_time_timer(tagged.trade.t_receipt_ns // 1_000_000)

    def process_element2(self, gs: GameStateEvent, ctx: Any) -> None:  # game states
        joiner = self._joiner()
        joiner.on_game_state(gs)
        self._state.update(joiner)
        ctx.timer_service().register_event_time_timer(gs.t_receipt_ns // 1_000_000)

    def on_timer(self, timestamp: int, ctx: Any) -> Generator[str, None, None]:
        joiner = self._joiner()
        watermark_ns = (timestamp + 1) * 1_000_000 - 1
        enriched = joiner.advance_watermark(watermark_ns)
        self._state.update(joiner)
        for e in enriched:
            yield e.model_dump_json()


# ─── Job wiring ───────────────────────────────────────────────────────────────


def _kafka_source(topic: str, bootstrap: str, bounded: bool) -> KafkaSource:
    builder = (
        KafkaSource.builder()
        .set_bootstrap_servers(bootstrap)
        .set_topics(topic)
        .set_group_id(f"enrich-{topic}")
        .set_starting_offsets(KafkaOffsetsInitializer.earliest())
        .set_value_only_deserializer(SimpleStringSchema())
    )
    if bounded:
        builder = builder.set_bounded(KafkaOffsetsInitializer.latest())
    return builder.build()


def _watermarks(out_of_orderness_s: int) -> WatermarkStrategy:
    return WatermarkStrategy.for_bounded_out_of_orderness(
        Duration.of_seconds(out_of_orderness_s)
    ).with_idleness(Duration.of_seconds(60))


def _ensure_output_topics(bootstrap: str) -> None:
    from pm.replay.producer import KafkaProducer

    KafkaProducer(bootstrap, topic_partitions={ENRICHED_TOPIC: 4, DLQ_TOPIC: 1})


def _kafka_sink(bootstrap: str, topic: str) -> KafkaSink:
    return (
        KafkaSink.builder()
        .set_bootstrap_servers(bootstrap)
        .set_record_serializer(
            KafkaRecordSerializationSchema.builder()
            .set_topic(topic)
            .set_value_serialization_schema(SimpleStringSchema())
            .build()
        )
        .build()
    )


def build_and_run(bootstrap: str, game_map: dict[str, str], bounded: bool = True) -> None:
    _ensure_output_topics(bootstrap)

    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)  # single-process dev; raise with the docker cluster
    # Python workers must use this venv's interpreter (pyflink + pm installed),
    # not the system python:
    env.set_python_executable(sys.executable)
    for jar in JARS_DIR.glob("*.jar"):
        env.add_jars(f"file://{jar}")

    dlq_sink = _kafka_sink(bootstrap, DLQ_TOPIC)

    trades_classified = env.from_source(
        _kafka_source("kalshi.trades", bootstrap, bounded),
        _watermarks(out_of_orderness_s=2),  # Kalshi WS is near-real-time
        "kalshi.trades",
    ).map(lambda raw: classify_trade(raw, game_map))
    trades = trades_classified.filter(lambda kp: kp[0] == OK).map(lambda kp: kp[1])
    trades_classified.filter(lambda kp: kp[0] == DLQ).map(
        lambda kp: kp[1], output_type=Types.STRING()
    ).sink_to(dlq_sink)

    gs_classified = env.from_source(
        _kafka_source("nba.game_state", bootstrap, bounded),
        _watermarks(out_of_orderness_s=30),  # CDN poll interval
        "nba.game_state",
    ).map(classify_game_state)
    game_states = gs_classified.filter(lambda kp: kp[0] == OK).map(lambda kp: kp[1])
    gs_classified.filter(lambda kp: kp[0] == DLQ).map(
        lambda kp: kp[1], output_type=Types.STRING()
    ).sink_to(dlq_sink)

    enriched = (
        trades.key_by(lambda t: t.game_id, key_type=Types.STRING())
        .connect(game_states.key_by(lambda g: g.game_id, key_type=Types.STRING()))
        .process(AsOfJoinOperator(), output_type=Types.STRING())
    )
    enriched.sink_to(_kafka_sink(bootstrap, ENRICHED_TOPIC))

    env.execute("enrich")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Run the enrichment job")
    parser.add_argument("--bootstrap-servers", default="localhost:9092")
    parser.add_argument(
        "--game-map",
        default='{"26APR18ATLNYK": "0042500121"}',
        help="JSON mapping of Kalshi event codes to NBA game_ids",
    )
    parser.add_argument(
        "--unbounded", action="store_true", help="keep consuming (live mode) instead of bounded"
    )
    args = parser.parse_args()

    build_and_run(
        bootstrap=args.bootstrap_servers,
        game_map=json.loads(args.game_map),
        bounded=not args.unbounded,
    )


if __name__ == "__main__":
    main()
