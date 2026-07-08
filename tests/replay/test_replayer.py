"""Tests for pm.replay.replayer using InMemoryProducer and a fake clock."""

from pathlib import Path

import pytest

from pm.replay.producer import InMemoryProducer
from pm.replay.replayer import EventReplayer
from pm.replay.sources import LocalFileSource, SourceRecord

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden"


class ListSource:
    def __init__(self, records: list[SourceRecord], source_id: str = "test") -> None:
        self._records = records
        self._source_id = source_id

    @property
    def source_id(self) -> str:
        return self._source_id

    def records(self):
        yield from self._records


def rec(ts_ns: int, topic: str = "kalshi.trades", key: str = "K") -> SourceRecord:
    return SourceRecord(t_receipt_ns=ts_ns, topic=topic, key=key, raw=b"{}")


class FakeClock:
    """Deterministic clock: sleep() advances time instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestReplayGolden:
    def test_golden_replay_counts_and_order(self) -> None:
        producer = InMemoryProducer()
        replayer = EventReplayer(producer)
        stats = replayer.replay(
            [
                LocalFileSource(GOLDEN / "nba_pbp_0042500121.jsonl.gz", "live_pbp"),
                LocalFileSource(GOLDEN / "kalshi_trades_0042500121.jsonl.gz", "trade"),
            ]
        )

        assert stats.total_records == 548 + 20376
        assert stats.records_per_topic == {
            "nba.game_state": 548,
            "kalshi.trades": 20376,
        }
        assert producer.flush_count == 1

        # Kafka message timestamps must be non-decreasing per topic
        for records in producer.produced.values():
            timestamps = [r.timestamp_ms for r in records]
            assert timestamps == sorted(timestamps)

    def test_golden_replay_keys(self) -> None:
        producer = InMemoryProducer()
        EventReplayer(producer).replay(
            [LocalFileSource(GOLDEN / "nba_pbp_0042500121.jsonl.gz", "live_pbp")]
        )
        keys = {r.key for r in producer.produced["nba.game_state"]}
        assert keys == {"0042500121"}

    def test_stats_time_span(self) -> None:
        producer = InMemoryProducer()
        stats = EventReplayer(producer).replay(
            [LocalFileSource(GOLDEN / "nba_pbp_0042500121.jsonl.gz", "live_pbp")]
        )
        assert stats.min_t_receipt_ns is not None
        assert stats.max_t_receipt_ns is not None
        span_hours = (stats.max_t_receipt_ns - stats.min_t_receipt_ns) / 1e9 / 3600
        assert 2.0 < span_hours < 3.0  # game ran 22:19 -> 00:50 UTC (~2.5h)


class TestPacing:
    def test_speed_zero_never_sleeps(self) -> None:
        fake = FakeClock()
        producer = InMemoryProducer()
        replayer = EventReplayer(producer, clock=fake.clock, sleep=fake.sleep)
        replayer.replay(
            [ListSource([rec(1_000_000_000), rec(5_000_000_000)])], speed_multiplier=0.0
        )
        assert fake.sleeps == []

    def test_real_time_pacing_sleeps_between_records(self) -> None:
        fake = FakeClock()
        producer = InMemoryProducer()
        replayer = EventReplayer(producer, clock=fake.clock, sleep=fake.sleep)
        # Two records 4 seconds apart in event time
        replayer.replay(
            [ListSource([rec(1_000_000_000), rec(5_000_000_000)])], speed_multiplier=1.0
        )
        assert fake.sleeps == pytest.approx([4.0])

    def test_double_speed_halves_sleep(self) -> None:
        fake = FakeClock()
        producer = InMemoryProducer()
        replayer = EventReplayer(producer, clock=fake.clock, sleep=fake.sleep)
        replayer.replay(
            [ListSource([rec(1_000_000_000), rec(5_000_000_000)])], speed_multiplier=2.0
        )
        assert fake.sleeps == pytest.approx([2.0])

    def test_negative_speed_rejected(self) -> None:
        replayer = EventReplayer(InMemoryProducer())
        with pytest.raises(ValueError):
            replayer.replay([], speed_multiplier=-1.0)


class TestEmptyReplay:
    def test_no_sources(self) -> None:
        producer = InMemoryProducer()
        stats = EventReplayer(producer).replay([])
        assert stats.total_records == 0
        assert stats.records_per_topic == {}
        assert producer.flush_count == 1
