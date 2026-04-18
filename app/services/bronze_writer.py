"""Async batched gzip-JSONL writer for bronze S3.

One instance per live process, bound to a `source` (e.g. "kalshi_ws", "nba_cdn").
Buffers records per `channel` in memory; flushes on size, time, or shutdown.

Usage:

    async with BronzeWriter(source="kalshi_ws") as bronze:
        await bronze.emit({"channel": "trade", ...}, channel="trade")
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import boto3

from app.core.config import S3_BUCKET

log = logging.getLogger(__name__)

DEFAULT_FLUSH_BYTES = 5 * 1024 * 1024
DEFAULT_FLUSH_SECONDS = 60


class BronzeWriter:
    def __init__(
        self,
        source: str,
        *,
        flush_bytes: int = DEFAULT_FLUSH_BYTES,
        flush_seconds: int = DEFAULT_FLUSH_SECONDS,
        bucket: str = S3_BUCKET,
        s3_client=None,
    ) -> None:
        self._source = source
        self._flush_bytes = flush_bytes
        self._flush_seconds = flush_seconds
        self._bucket = bucket
        self._s3 = s3_client or boto3.client("s3")

        self._buffers: dict[str, list[bytes]] = defaultdict(list)
        self._sizes: dict[str, int] = defaultdict(int)
        self._flush_task: asyncio.Task | None = None

    async def __aenter__(self) -> "BronzeWriter":
        self._flush_task = asyncio.create_task(self._flush_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        await self._flush_all()

    async def emit(self, record: dict, *, channel: str) -> None:
        line = json.dumps(record, separators=(",", ":"), default=str).encode() + b"\n"
        self._buffers[channel].append(line)
        self._sizes[channel] += len(line)
        if self._sizes[channel] >= self._flush_bytes:
            await self._flush_channel(channel)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_seconds)
            try:
                await self._flush_all()
            except Exception:
                log.exception("bronze flush_loop error")

    async def _flush_all(self) -> None:
        for channel in list(self._buffers.keys()):
            await self._flush_channel(channel)

    async def _flush_channel(self, channel: str) -> str | None:
        lines = self._buffers[channel]
        if not lines:
            return None
        self._buffers[channel] = []
        self._sizes[channel] = 0

        body = gzip.compress(b"".join(lines))
        now = datetime.now(timezone.utc)
        key = (
            f"bronze/{self._source}/{channel}/"
            f"{now:%Y/%m/%d/%H}/{uuid.uuid4().hex}.jsonl.gz"
        )
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/gzip",
        )
        log.info("bronze flush %s/%s → %s (%d records, %d bytes)",
                 self._source, channel, key, len(lines), len(body))
        return key
