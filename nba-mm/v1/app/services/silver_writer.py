"""Async batched Parquet writer for silver S3.

One instance per live process, bound to a `source`. Buffers typed events by
their class name; flushes every `flush_seconds` (default 5 min) or on shutdown.

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

from app.core.config import S3_BUCKET, SILVER_VERSION
from app.events import Event

log = logging.getLogger(__name__)

DEFAULT_FLUSH_SECONDS = 300


class SilverWriter:
    def __init__(
        self,
        source: str,
        *,
        flush_seconds: int = DEFAULT_FLUSH_SECONDS,
        bucket: str = S3_BUCKET,
        version: int = SILVER_VERSION,
        s3_client=None,
    ) -> None:
        self._source = source
        self._flush_seconds = flush_seconds
        self._bucket = bucket
        self._version = version
        self._s3 = s3_client or boto3.client("s3")

        self._buffers: dict[str, list[Event]] = defaultdict(list)
        self._flush_task: asyncio.Task | None = None

    async def __aenter__(self) -> "SilverWriter":
        self._flush_task = asyncio.create_task(self._flush_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        await self._flush_all()

    async def emit(self, event: Event) -> None:
        self._buffers[type(event).__name__].append(event)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_seconds)
            try:
                await self._flush_all()
            except Exception:
                log.exception("silver flush_loop error")

    async def _flush_all(self) -> None:
        for type_name in list(self._buffers.keys()):
            await self._flush_type(type_name)

    async def _flush_type(self, type_name: str) -> str | None:
        events = self._buffers[type_name]
        if not events:
            return None
        self._buffers[type_name] = []

        rows = [asdict(e) for e in events]
        table = pa.Table.from_pylist(rows)
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink, compression="zstd")
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
