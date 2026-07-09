"""Inspect the dead-letter queue (dlq.enrich) topic.

Usage:
    python scripts/inspect_dlq.py          # count + first 5 records
    python scripts/inspect_dlq.py -n 20    # count + first 20 records
    python scripts/inspect_dlq.py -n 0     # count only
"""

import argparse
import json
import sys

from confluent_kafka import Consumer, TopicPartition

TOPIC = "dlq.enrich"


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the enrich DLQ topic")
    parser.add_argument("-n", type=int, default=5, help="number of records to show (0=count only)")
    parser.add_argument("--bootstrap-servers", default="localhost:9092")
    args = parser.parse_args()

    c = Consumer(
        {"bootstrap.servers": args.bootstrap_servers, "group.id": "dlq-inspect",
         "auto.offset.reset": "earliest"}
    )

    meta = c.list_topics(TOPIC, timeout=5).topics.get(TOPIC)
    if meta is None:
        print(f"{TOPIC}: topic does not exist")
        c.close()
        return

    total = sum(
        c.get_watermark_offsets(TopicPartition(TOPIC, p), timeout=5)[1]
        - c.get_watermark_offsets(TopicPartition(TOPIC, p), timeout=5)[0]
        for p in meta.partitions
    )
    print(f"{TOPIC}: {total:,} records")

    if total == 0 or args.n == 0:
        c.close()
        return

    c.assign([TopicPartition(TOPIC, p, 0) for p in meta.partitions])
    shown = 0
    while shown < args.n:
        msg = c.poll(5)
        if msg is None:
            break
        rec = json.loads(msg.value())
        print(f"\n--- record {shown + 1} (partition {msg.partition()}, offset {msg.offset()}) ---")
        print(f"  source: {rec.get('source_topic')}")
        print(f"  error:  {rec.get('error')}")
        raw = rec.get("raw", "")
        print(f"  raw:    {raw[:200]}{'...' if len(raw) > 200 else ''}")
        shown += 1
    c.close()


if __name__ == "__main__":
    main()
