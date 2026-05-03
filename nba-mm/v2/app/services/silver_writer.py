"""Async batched Parquet writer for silver S3 (v2).

Changes from v1:
- Explicit Arrow schemas per event type (no inference)
- Dictionary encoding for string columns (market_ticker, side, etc.)
- t_receipt (float seconds) → t_receipt_ns (int64 nanoseconds) at serialization
- Rows sorted by t_receipt_ns before writing
- row_group_size=100_000 for predicate pushdown
- min_rows flush guard to reduce tiny-file proliferation

Usage:

    async with SilverWriter(source="kalshi_ws") as silver:
        await silver.emit(OrderBookUpdate(...))
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from v2.app.core.config import S3_BUCKET, SILVER_VERSION
from v2.app.events import Event

log = logging.getLogger(__name__)

DEFAULT_FLUSH_SECONDS = 300
DEFAULT_MIN_ROWS = 1000
ROW_GROUP_SIZE = 100_000

# ---------------------------------------------------------------------------
# Helper types
# ---------------------------------------------------------------------------
_dict_utf8 = pa.dictionary(pa.int16(), pa.utf8())


# ---------------------------------------------------------------------------
# Explicit schemas — one per event type
# ---------------------------------------------------------------------------
SCHEMAS: dict[str, pa.Schema] = {
    "OrderBookUpdate": pa.schema([
        ("t_receipt_ns", pa.int64()),
        ("t_exchange_ns", pa.int64()),       # nullable — None for snapshots
        ("market_ticker", _dict_utf8),
        ("bid_yes", pa.int32()),
        ("ask_yes", pa.int32()),
        ("bid_size", pa.int32()),
        ("ask_size", pa.int32()),
        ("sid", pa.int32()),                 # nullable — subscription ID
        ("seq", pa.int32()),                 # nullable — sequence number
    ]),
    "TradeEvent": pa.schema([
        ("t_receipt_ns", pa.int64()),
        ("t_exchange_ns", pa.int64()),       # nullable
        ("market_ticker", _dict_utf8),
        ("side", _dict_utf8),
        ("price", pa.int32()),
        ("size", pa.int32()),
        ("sid", pa.int32()),                 # nullable
        ("seq", pa.int32()),                 # nullable
    ]),
    "BookInvalidated": pa.schema([
        ("t_receipt_ns", pa.int64()),
        ("market_ticker", _dict_utf8),
    ]),
    "MMQuoteEvent": pa.schema([
        ("t_receipt_ns", pa.int64()),
        ("market_ticker", _dict_utf8),
        ("bid_price", pa.int32()),     # nullable
        ("ask_price", pa.int32()),     # nullable
        ("book_bid", pa.int32()),
        ("book_ask", pa.int32()),
        ("spread", pa.int32()),
        ("position", pa.int32()),
        ("reason_no_bid", _dict_utf8),  # nullable
        ("reason_no_ask", _dict_utf8),  # nullable
    ]),
    "MMOrderEvent": pa.schema([
        ("t_receipt_ns", pa.int64()),
        ("market_ticker", _dict_utf8),
        ("action", _dict_utf8),
        ("price", pa.int32()),         # nullable
        ("size", pa.int32()),          # nullable
        ("order_id", pa.utf8()),       # nullable, high cardinality — no dict
        ("reason", _dict_utf8),
        ("error", pa.utf8()),          # nullable, variable — no dict
    ]),
    "MMFillEvent": pa.schema([
        ("t_receipt_ns", pa.int64()),
        ("market_ticker", _dict_utf8),
        ("side", _dict_utf8),
        ("price", pa.int32()),
        ("fill_size", pa.int32()),
        ("order_remaining_size", pa.int32()),
        ("position_before", pa.int32()),
        ("position_after", pa.int32()),
        ("maker_fee", pa.int32()),
        ("order_id", pa.utf8()),       # high cardinality — no dict
        ("book_mid_at_fill", pa.int32()),
    ]),
    "MMReconcileEvent": pa.schema([
        ("t_receipt_ns", pa.int64()),
        ("market_ticker", _dict_utf8),
        ("field", _dict_utf8),
        ("internal_value", pa.utf8()),
        ("actual_value", pa.utf8()),
        ("action_taken", _dict_utf8),
    ]),
    "MMCircuitBreakerEvent": pa.schema([
        ("t_receipt_ns", pa.int64()),
        ("state", _dict_utf8),
        ("consecutive_failures", pa.int32()),
        ("last_error", pa.utf8()),     # nullable
    ]),
    "OrderBookDepth": pa.schema(
        [
            ("t_receipt_ns", pa.int64()),
            ("t_exchange_ns", pa.int64()),
            ("market_ticker", _dict_utf8),
            ("seq", pa.int32()),
            ("sid", pa.int32()),
        ]
        + [
            col
            for i in range(1, 11)
            for col in [
                (f"bid_{i}", pa.int32()),
                (f"bid_{i}_size", pa.int32()),
            ]
        ]
        + [
            col
            for i in range(1, 11)
            for col in [
                (f"ask_{i}", pa.int32()),
                (f"ask_{i}_size", pa.int32()),
            ]
        ]
        + [
            ("bid_depth_5c", pa.int32()),
            ("ask_depth_5c", pa.int32()),
            ("bid_depth_10c", pa.int32()),
            ("ask_depth_10c", pa.int32()),
            ("num_bid_levels", pa.int32()),
            ("num_ask_levels", pa.int32()),
            ("spread", pa.int32()),
            ("mid_x2", pa.int32()),
        ]
    ),
}


def _prepare_rows(events: list[Event]) -> list[dict]:
    """Convert event dataclasses to dicts with int64 ns timestamps."""
    rows = []
    for e in events:
        row = asdict(e)
        row["t_receipt_ns"] = int(row.pop("t_receipt") * 1_000_000_000)
        # Convert t_exchange (float seconds | None) → t_exchange_ns (int64 | None)
        t_ex = row.pop("t_exchange", None)
        row["t_exchange_ns"] = int(t_ex * 1_000_000_000) if t_ex is not None else None
        rows.append(row)
    rows.sort(key=lambda r: r["t_receipt_ns"])
    return rows


class SilverWriter:
    def __init__(
        self,
        source: str,
        *,
        flush_seconds: int = DEFAULT_FLUSH_SECONDS,
        min_rows: int = DEFAULT_MIN_ROWS,
        bucket: str = S3_BUCKET,
        version: int = SILVER_VERSION,
        s3_client=None,
    ) -> None:
        self._source = source
        self._flush_seconds = flush_seconds
        self._min_rows = min_rows
        self._bucket = bucket
        self._version = version
        self._s3 = s3_client or boto3.client("s3")

        self._buffers: dict[str, list[Event]] = defaultdict(list)
        self._raw_buffers: dict[str, list[dict]] = defaultdict(list)
        self._flush_task: asyncio.Task | None = None
        self._draining = False

    async def __aenter__(self) -> "SilverWriter":
        self._flush_task = asyncio.create_task(self._flush_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        self._draining = True
        await self._flush_all()
        self._draining = False

    async def emit(self, event: Event) -> None:
        self._buffers[type(event).__name__].append(event)

    async def emit_row(self, type_name: str, row: dict) -> None:
        """Emit a pre-formatted row dict (timestamps already in nanoseconds)."""
        self._raw_buffers[type_name].append(row)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_seconds)
            try:
                await self._flush_all()
            except Exception:
                log.exception("silver flush_loop error")

    async def _flush_all(self) -> None:
        all_types = set(self._buffers.keys()) | set(self._raw_buffers.keys())
        for type_name in list(all_types):
            await self._flush_type(type_name)

    async def _flush_type(self, type_name: str) -> str | None:
        events = self._buffers.get(type_name, [])
        raw_rows = self._raw_buffers.get(type_name, [])
        total = len(events) + len(raw_rows)
        if total == 0:
            return None

        # Min-rows guard: skip flush unless draining (shutdown)
        if total < self._min_rows and not self._draining:
            return None

        self._buffers[type_name] = []
        self._raw_buffers[type_name] = []

        # Event dataclasses need asdict + timestamp conversion.
        # Raw dicts are already in the correct format.
        rows = _prepare_rows(events) if events else []
        rows.extend(raw_rows)
        rows.sort(key=lambda r: r["t_receipt_ns"])
        schema = SCHEMAS.get(type_name)
        if schema is not None:
            table = pa.Table.from_pylist(rows, schema=schema)
        else:
            # Unknown event type — fall back to inference
            log.warning("no explicit schema for %s, using inference", type_name)
            table = pa.Table.from_pylist(rows)

        sink = pa.BufferOutputStream()
        pq.write_table(
            table,
            sink,
            compression="zstd",
            row_group_size=ROW_GROUP_SIZE,
        )
        body = sink.getvalue().to_pybytes()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (
            f"silver/{self._source}/{type_name}/"
            f"date={today}/v={self._version}/part-{uuid.uuid4().hex}.parquet"
        )
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/octet-stream",
        )
        log.info("silver flush %s/%s → %s (%d rows, %d bytes)",
                 self._source, type_name, key, len(events), len(body))
        return key
