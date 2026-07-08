"""Producer abstraction for the replayer (DESIGN.md §1).

The replayer writes through a Producer protocol so the merge/pacing logic is
testable without a broker:

- KafkaProducer:    real confluent-kafka producer; creates topics with the
                    configured partition counts on startup.
- InMemoryProducer: unit tests; records everything produced, in order.

The Kafka message timestamp is set to the record's receipt time in
milliseconds — this is what Flink reads as event time (DESIGN.md D2: replayed
history must be indistinguishable from live data).
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol

# Topic -> partition count (DESIGN.md: 4/4/1/1)
DEFAULT_TOPIC_PARTITIONS: dict[str, int] = {
    "kalshi.trades": 4,
    "kalshi.book_update": 4,
    "nba.game_state": 1,
    "reference.markets": 1,
}


@dataclass(frozen=True)
class ProducerRecord:
    topic: str
    key: str
    value: bytes
    timestamp_ms: int


class Producer(Protocol):
    def produce(self, record: ProducerRecord) -> None: ...

    def flush(self) -> None: ...


class InMemoryProducer:
    """Collects produced records per topic. For unit tests."""

    def __init__(self) -> None:
        self.produced: dict[str, list[ProducerRecord]] = defaultdict(list)
        self.flush_count = 0

    def produce(self, record: ProducerRecord) -> None:
        self.produced[record.topic].append(record)

    def flush(self) -> None:
        self.flush_count += 1


class KafkaProducer:
    """Wraps confluent_kafka.Producer with topic creation.

    Topics are created idempotently at construction with the partition counts
    from topic_partitions. Auto-creation on the broker is disabled
    (docker-compose sets KAFKA_AUTO_CREATE_TOPICS_ENABLE=false) so partition
    counts are always explicit — key-to-partition mapping must be stable.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        topic_partitions: dict[str, int] | None = None,
    ) -> None:
        from confluent_kafka import Producer as ConfluentProducer
        from confluent_kafka.admin import AdminClient, NewTopic

        self._topic_partitions = topic_partitions or DEFAULT_TOPIC_PARTITIONS

        admin = AdminClient({"bootstrap.servers": bootstrap_servers})
        existing = set(admin.list_topics(timeout=10).topics)
        to_create = [
            NewTopic(
                topic,
                num_partitions=partitions,
                replication_factor=1,
                # Infinite retention: replayed records carry their ORIGINAL
                # timestamps (months old). Kafka's time-based retention uses
                # message timestamps, so default 7-day retention would delete
                # a historical replay within minutes of segment roll. Topics
                # are rebuildable via replay, so unbounded retention is safe.
                config={"retention.ms": "-1"},
            )
            for topic, partitions in self._topic_partitions.items()
            if topic not in existing
        ]
        if to_create:
            futures = admin.create_topics(to_create)
            for topic, future in futures.items():
                future.result(timeout=30)  # raises on failure

        self._producer = ConfluentProducer(
            {
                "bootstrap.servers": bootstrap_servers,
                # Preserve produce order per partition even on retry:
                "enable.idempotence": True,
                # Throughput for full-speed replay:
                "linger.ms": 50,
                "batch.size": 1_000_000,
                "compression.type": "zstd",
            }
        )

    def produce(self, record: ProducerRecord) -> None:
        # poll(0) services delivery callbacks and frees queue space; without it
        # a full-speed replay overruns the local buffer.
        self._producer.produce(
            topic=record.topic,
            key=record.key.encode(),
            value=record.value,
            timestamp=record.timestamp_ms,
        )
        self._producer.poll(0)

    def flush(self) -> None:
        remaining = self._producer.flush(timeout=60)
        if remaining:
            raise RuntimeError(f"{remaining} records unflushed after timeout")
