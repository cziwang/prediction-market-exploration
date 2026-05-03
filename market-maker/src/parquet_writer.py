"""Buffered Parquet writer that flushes to S3."""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

S3_BUCKET = "prediction-markets-data"
S3_PREFIX = "mm/bronze"


# -- Schemas -------------------------------------------------------------

DELTA_SCHEMA = pa.schema([
    ("market_ticker", pa.string()),
    ("ts", pa.int64()),          # epoch ms
    ("side", pa.string()),       # "yes" | "no"
    ("price_cents", pa.int32()),
    ("delta", pa.int32()),       # signed
    ("seq", pa.int64()),
    ("sid", pa.int64()),
])

SNAPSHOT_SCHEMA = pa.schema([
    ("market_ticker", pa.string()),
    ("ts", pa.int64()),          # epoch ms
    ("side", pa.string()),
    ("price_cents", pa.int32()),
    ("qty", pa.int32()),
])

TRADE_SCHEMA = pa.schema([
    ("market_ticker", pa.string()),
    ("ts", pa.int64()),
    ("yes_price_cents", pa.int32()),
    ("no_price_cents", pa.int32()),
    ("count", pa.int32()),
    ("taker_side", pa.string()),
])


@dataclass
class WriterConfig:
    bucket: str = S3_BUCKET
    prefix: str = S3_PREFIX
    flush_interval_s: float = 30.0
    flush_row_limit: int = 50_000


class ParquetBuffer:
    """Accumulates rows for a single stream and flushes to S3 as Parquet."""

    def __init__(
        self,
        stream_name: str,
        schema: pa.Schema,
        config: WriterConfig,
        s3_client=None,
    ):
        self.stream_name = stream_name
        self.schema = schema
        self.config = config
        self._s3 = s3_client or boto3.client("s3")
        self._rows: dict[str, list] = {f.name: [] for f in schema}
        self._count = 0
        self._last_flush = time.time()

    def append(self, row: dict) -> None:
        """Append a single row. Flushes automatically if thresholds are hit."""
        for col in self.schema.names:
            self._rows[col].append(row.get(col))
        self._count += 1

        if self._should_flush():
            self.flush()

    def _should_flush(self) -> bool:
        if self._count >= self.config.flush_row_limit:
            return True
        if time.time() - self._last_flush >= self.config.flush_interval_s:
            return True
        return False

    def flush(self) -> str | None:
        """Write buffered rows to S3 as a Parquet file. Returns the S3 key."""
        if self._count == 0:
            return None

        table = pa.table(self._rows, schema=self.schema)

        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd")
        buf.seek(0)

        now = datetime.now(timezone.utc)
        dt_str = now.strftime("%Y-%m-%d")
        ts_str = now.strftime("%H%M%S_%f")
        key = f"{self.config.prefix}/{self.stream_name}/dt={dt_str}/{ts_str}.parquet"

        self._s3.put_object(
            Bucket=self.config.bucket,
            Key=key,
            Body=buf.getvalue(),
        )

        log.info(
            "Flushed %d %s rows → s3://%s/%s",
            self._count,
            self.stream_name,
            self.config.bucket,
            key,
        )

        # Reset buffer.
        self._rows = {f.name: [] for f in self.schema}
        self._count = 0
        self._last_flush = time.time()
        return key

    @property
    def pending_count(self) -> int:
        return self._count


@dataclass
class BronzeWriter:
    """Manages three ParquetBuffers for deltas, snapshots, and trades."""

    config: WriterConfig = field(default_factory=WriterConfig)
    _deltas: ParquetBuffer = field(init=False)
    _snapshots: ParquetBuffer = field(init=False)
    _trades: ParquetBuffer = field(init=False)

    def __post_init__(self):
        self._deltas = ParquetBuffer("deltas", DELTA_SCHEMA, self.config)
        self._snapshots = ParquetBuffer("snapshots", SNAPSHOT_SCHEMA, self.config)
        self._trades = ParquetBuffer("trades", TRADE_SCHEMA, self.config)

    def write_delta(self, row: dict) -> None:
        self._deltas.append(row)

    def write_snapshot_levels(self, parsed: dict) -> None:
        """Write a parsed REST snapshot (one row per price level)."""
        ticker = parsed["market_ticker"]
        ts = parsed["ts"]
        for level in parsed["levels"]:
            self._snapshots.append({
                "market_ticker": ticker,
                "ts": ts,
                "side": level["side"],
                "price_cents": level["price_cents"],
                "qty": level["qty"],
            })

    def write_trade(self, row: dict) -> None:
        self._trades.append(row)

    def flush_all(self) -> None:
        self._deltas.flush()
        self._snapshots.flush()
        self._trades.flush()
