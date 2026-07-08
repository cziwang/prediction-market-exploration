"""Tests for pm.replay.producer (in-memory implementation only; KafkaProducer
is covered by the integration test once docker compose is up)."""

from pm.replay.producer import DEFAULT_TOPIC_PARTITIONS, InMemoryProducer, ProducerRecord


class TestInMemoryProducer:
    def test_records_stored_per_topic_in_order(self) -> None:
        producer = InMemoryProducer()
        r1 = ProducerRecord("kalshi.trades", "TICKER-A", b"{}", 1000)
        r2 = ProducerRecord("nba.game_state", "0042500121", b"{}", 1001)
        r3 = ProducerRecord("kalshi.trades", "TICKER-B", b"{}", 1002)

        for r in (r1, r2, r3):
            producer.produce(r)

        assert producer.produced["kalshi.trades"] == [r1, r3]
        assert producer.produced["nba.game_state"] == [r2]

    def test_flush_counted(self) -> None:
        producer = InMemoryProducer()
        producer.flush()
        producer.flush()
        assert producer.flush_count == 2


class TestTopicConfig:
    def test_default_partitions_match_design(self) -> None:
        # DESIGN.md: 4/4/1/1
        assert DEFAULT_TOPIC_PARTITIONS == {
            "kalshi.trades": 4,
            "kalshi.book_update": 4,
            "nba.game_state": 1,
            "reference.markets": 1,
        }
