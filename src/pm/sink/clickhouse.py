"""Kafka -> ClickHouse sink for enriched trades (DESIGN.md §4, D3, D5).

A standalone Kafka consumer (D7: Kafka is the data plane — ClickHouse
ingestion is just another consumer of enriched.trades). At-least-once
delivery with idempotent inserts:

- Batches are OFFSET-ALIGNED per partition: a batch flushes when it reaches
  an offset boundary (last_offset + 1 divisible by batch_size), so batch
  boundaries are deterministic regardless of timing or restarts.
- Each insert carries insert_deduplication_token = "{partition}-{first}-{last}".
  Reprocessing after a crash (offsets not yet committed) re-forms the exact
  same batch with the same token, and ClickHouse drops it as a duplicate.
- Offsets are committed only AFTER a successful insert.
- MergeTree note: non-replicated tables ignore dedup tokens unless
  non_replicated_deduplication_window is set on the table — the DDL sets it.

Corner case (documented, accepted): the final partial batch in --once mode is
bounded by the end offsets at run time; if a crash-restart races new data,
its token may differ and overlap rows can duplicate. The full-batch path —
the analog of Flink checkpoint reprocessing — is exactly idempotent, and the
chaos test verifies it.

Usage:
    python -m pm.sink.clickhouse --once      # consume to current end, then exit
    python -m pm.sink.clickhouse             # run forever (live mode)
"""

import argparse
import json
import logging
from typing import Any

import clickhouse_connect
from confluent_kafka import Consumer, TopicPartition

log = logging.getLogger(__name__)

TOPIC = "enriched.trades"
TABLE = "enriched_trades"

DDL = """
CREATE TABLE IF NOT EXISTS enriched_trades (
    t_trade_receipt_ns     Int64,
    t_trade_receipt        DateTime64(3) MATERIALIZED fromUnixTimestamp64Nano(t_trade_receipt_ns),
    t_trade_exchange_ns    Nullable(Int64),
    schema_version         UInt8,
    market_ticker          LowCardinality(String),
    series                 LowCardinality(String),
    game_id                String,
    trade_price            Int32,
    trade_size_cc          Int32,
    taker_side             LowCardinality(String),

    r_period               Nullable(Int32),
    r_seconds_remaining    Nullable(Float32),
    r_score_diff           Nullable(Int32),
    r_model_prob           Nullable(Float32),
    r_edge                 Nullable(Float32),
    r_info_delay_ms        Nullable(Int64),

    e_period               Nullable(Int32),
    e_seconds_remaining    Nullable(Float32),
    e_score_diff           Nullable(Int32),
    e_model_prob           Nullable(Float32),
    e_edge                 Nullable(Float32),
    e_info_delay_ms        Nullable(Int64),

    market_implied_prob    Float32
) ENGINE = MergeTree
ORDER BY (series, game_id, t_trade_receipt_ns)
SETTINGS non_replicated_deduplication_window = 1000
"""

COLUMNS = [
    "t_trade_receipt_ns",
    "t_trade_exchange_ns",
    "schema_version",
    "market_ticker",
    "series",
    "game_id",
    "trade_price",
    "trade_size_cc",
    "taker_side",
    "r_period",
    "r_seconds_remaining",
    "r_score_diff",
    "r_model_prob",
    "r_edge",
    "r_info_delay_ms",
    "e_period",
    "e_seconds_remaining",
    "e_score_diff",
    "e_model_prob",
    "e_edge",
    "e_info_delay_ms",
    "market_implied_prob",
]


class ClickHouseSink:
    def __init__(
        self,
        bootstrap: str = "localhost:9092",
        ch_host: str = "localhost",
        ch_port: int = 8123,
        batch_size: int = 5000,
        group_id: str = "ch-sink",
    ) -> None:
        self.batch_size = batch_size
        self.ch = clickhouse_connect.get_client(
            host=ch_host, port=ch_port, username="pm", password="pm", database="pm"
        )
        self.ch.command(DDL)
        self.consumer = Consumer(
            {
                "bootstrap.servers": bootstrap,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,  # commit only after successful insert
            }
        )
        # Per-partition accumulators: partition -> (first_offset, rows)
        self._batches: dict[int, tuple[int, list[list[Any]]]] = {}

    def _row(self, record: dict[str, Any]) -> list[Any]:
        return [record.get(c) for c in COLUMNS]

    def _flush(self, partition: int, last_offset: int) -> None:
        first_offset, rows = self._batches.pop(partition)
        token = f"{TOPIC}-{partition}-{first_offset}-{last_offset}"
        self.ch.insert(
            TABLE,
            rows,
            column_names=COLUMNS,
            settings={"insert_deduplication_token": token},
        )
        # Commit AFTER the insert succeeded (at-least-once)
        self.consumer.commit(
            offsets=[TopicPartition(TOPIC, partition, last_offset + 1)], asynchronous=False
        )
        log.info("flushed p%d [%d..%d] (%d rows) token=%s",
                 partition, first_offset, last_offset, len(rows), token)

    def run(self, once: bool = False, poll_timeout_s: float = 1.0) -> int:
        """Consume and sink. Returns rows inserted. once=True: stop at end offsets."""
        meta = self.consumer.list_topics(TOPIC, timeout=10).topics[TOPIC]
        partitions = list(meta.partitions)
        ends = {
            p: self.consumer.get_watermark_offsets(TopicPartition(TOPIC, p), timeout=10)[1]
            for p in partitions
        }
        self.consumer.subscribe([TOPIC])

        inserted = 0
        done: set[int] = set()
        while True:
            msg = self.consumer.poll(poll_timeout_s)
            if msg is None:
                if once and all(
                    p in done or ends[p] == 0 for p in partitions
                ):
                    break
                continue
            if msg.error():
                raise RuntimeError(msg.error())

            p, offset = msg.partition(), msg.offset()
            record = json.loads(msg.value())
            if p not in self._batches:
                self._batches[p] = (offset, [])
            first, rows = self._batches[p]
            rows.append(self._row(record))
            inserted += 1

            # Offset-aligned flush: deterministic batch boundaries
            at_boundary = (offset + 1) % self.batch_size == 0
            at_end = once and offset + 1 >= ends[p]
            if at_boundary or at_end:
                self._flush(p, offset)
            if at_end:
                done.add(p)
            if once and all(p in done or ends[p] == 0 for p in partitions):
                break

        # Flush any tails (once mode reaches here with empty batches)
        for p, (first, rows) in list(self._batches.items()):
            if rows:
                self._flush(p, first + len(rows) - 1)
        self.consumer.close()
        return inserted


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Kafka -> ClickHouse sink")
    parser.add_argument("--bootstrap-servers", default="localhost:9092")
    parser.add_argument("--ch-host", default="localhost")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--group-id", default="ch-sink")
    parser.add_argument("--once", action="store_true", help="consume to current end, then exit")
    args = parser.parse_args()

    sink = ClickHouseSink(
        bootstrap=args.bootstrap_servers,
        ch_host=args.ch_host,
        batch_size=args.batch_size,
        group_id=args.group_id,
    )
    n = sink.run(once=args.once)
    print(f"inserted {n:,} rows")


if __name__ == "__main__":
    main()
