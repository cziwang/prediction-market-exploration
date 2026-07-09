"""Show message counts for all pipeline Kafka topics.

Usage: python scripts/topic_counts.py [topic1 topic2 ...]
       (no args = all pipeline topics)
"""

import sys

from confluent_kafka import Consumer, TopicPartition

DEFAULT_TOPICS = [
    "kalshi.trades",
    "kalshi.book_update",
    "nba.game_state",
    "reference.markets",
    "enriched.trades",
    "dlq.enrich",
]


def main() -> None:
    topics = sys.argv[1:] or DEFAULT_TOPICS
    c = Consumer({"bootstrap.servers": "localhost:9092", "group.id": "topic-counts"})
    for topic in topics:
        meta = c.list_topics(topic, timeout=5).topics.get(topic)
        if meta is None:
            print(f"{topic:25s} does not exist")
        else:
            total = sum(
                c.get_watermark_offsets(TopicPartition(topic, p), timeout=5)[1]
                - c.get_watermark_offsets(TopicPartition(topic, p), timeout=5)[0]
                for p in meta.partitions
            )
            print(f"{topic:25s} {total:>10,} msgs  ({len(meta.partitions)} partitions)")
    c.close()


if __name__ == "__main__":
    main()
