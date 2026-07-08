"""Historical event replayer (DESIGN.md §1).

Orchestrates: merge-sorted bronze sources -> optional real-time pacing ->
producer. The replayer does not normalize — raw bronze bytes are produced
with the original receipt timestamp so that, to any Kafka consumer,
replayed history is indistinguishable from a live feed (D2).

Pacing (speed_multiplier):
    0.0  — no sleeping, replay as fast as the producer allows (default)
    1.0  — reproduce original inter-record gaps (integration tests: drive
           the live stack in real time)
    2.0  — twice real-time speed, etc.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pm.replay.merge import merge_sources
from pm.replay.producer import Producer, ProducerRecord
from pm.replay.sources import BronzeSource


@dataclass
class ReplayStats:
    records_per_topic: dict[str, int] = field(default_factory=dict)
    total_records: int = 0
    min_t_receipt_ns: int | None = None
    max_t_receipt_ns: int | None = None
    wall_seconds: float = 0.0

    def summary(self) -> str:
        lines = [f"replayed {self.total_records:,} records in {self.wall_seconds:.1f}s"]
        for topic, count in sorted(self.records_per_topic.items()):
            lines.append(f"  {topic:20s} {count:>10,}")
        if self.min_t_receipt_ns is not None and self.max_t_receipt_ns is not None:
            span_s = (self.max_t_receipt_ns - self.min_t_receipt_ns) / 1e9
            lines.append(f"  event-time span: {span_s / 3600:.2f}h")
        return "\n".join(lines)


class EventReplayer:
    def __init__(
        self,
        producer: Producer,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """clock/sleep are injectable for deterministic pacing tests."""
        self._producer = producer
        self._clock = clock
        self._sleep = sleep

    def replay(
        self,
        sources: list[BronzeSource],
        speed_multiplier: float = 0.0,
    ) -> ReplayStats:
        if speed_multiplier < 0:
            raise ValueError("speed_multiplier must be >= 0")

        stats = ReplayStats()
        started_wall = self._clock()
        first_event_ns: int | None = None

        for record in merge_sources(sources):
            if first_event_ns is None:
                first_event_ns = record.t_receipt_ns
                stats.min_t_receipt_ns = record.t_receipt_ns

            if speed_multiplier > 0:
                # Sleep until this record is "due": the point where scaled
                # event-time elapsed matches wall-clock elapsed.
                event_elapsed_s = (record.t_receipt_ns - first_event_ns) / 1e9
                due_wall = started_wall + event_elapsed_s / speed_multiplier
                delay = due_wall - self._clock()
                if delay > 0:
                    self._sleep(delay)

            self._producer.produce(
                ProducerRecord(
                    topic=record.topic,
                    key=record.key,
                    value=record.raw,
                    timestamp_ms=record.t_receipt_ns // 1_000_000,
                )
            )
            stats.records_per_topic[record.topic] = (
                stats.records_per_topic.get(record.topic, 0) + 1
            )
            stats.total_records += 1
            stats.max_t_receipt_ns = record.t_receipt_ns

        self._producer.flush()
        stats.wall_seconds = self._clock() - started_wall
        return stats
