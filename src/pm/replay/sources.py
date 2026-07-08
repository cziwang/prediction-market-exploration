"""Bronze file sources for the historical event replayer (DESIGN.md §1).

A source reads one bronze .jsonl.gz file and yields SourceRecords in file
order. Each record carries the fields the replayer needs to produce it to
Kafka: the sort timestamp, the destination topic, and the partition key.

The raw line is passed through untouched — the replayer does not normalize.
Normalization happens downstream (Flink / consumers), keeping the replayer's
job purely: read, sort, partition, produce with correct timestamps.

Channel -> topic / key mapping:

    channel            topic               key
    ─────────          ─────────────────   ──────────────────────────
    trade              kalshi.trades       frame.msg.market_ticker
    orderbook_delta    kalshi.book_update  frame.msg.market_ticker
    live_pbp           nba.game_state      game_id (envelope field)
"""

import gzip
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

Channel = str  # "trade" | "orderbook_delta" | "live_pbp"

_CHANNEL_TOPIC: dict[Channel, str] = {
    "trade": "kalshi.trades",
    "orderbook_delta": "kalshi.book_update",
    "live_pbp": "nba.game_state",
}


class SourceError(Exception):
    """A source could not parse a line or extract required fields."""

    def __init__(self, source_id: str, line_no: int, reason: str) -> None:
        self.source_id = source_id
        self.line_no = line_no
        self.reason = reason
        super().__init__(f"{source_id}:{line_no}: {reason}")


@dataclass(frozen=True)
class SourceRecord:
    """One bronze record, ready to be merged and produced."""

    t_receipt_ns: int  # sort key (receipt time, nanoseconds)
    topic: str  # destination Kafka topic
    key: str  # Kafka partition key (market_ticker or game_id)
    raw: bytes  # the original JSON line, untouched


class BronzeSource(Protocol):
    """A single bronze file yielding records in file order."""

    @property
    def source_id(self) -> str: ...

    def records(self) -> Iterator[SourceRecord]: ...


def _extract_key(record: dict[str, Any], channel: Channel) -> str:
    key = record["game_id"] if channel == "live_pbp" else record["frame"]["msg"]["market_ticker"]
    if not isinstance(key, str) or not key:
        raise KeyError(f"empty or non-string key for channel {channel!r}")
    return key


def _parse_lines(
    lines: Iterator[bytes], channel: Channel, source_id: str
) -> Iterator[SourceRecord]:
    """Shared parsing logic for local and S3 sources.

    Raises SourceError on the first unparseable line: the replayer's input is
    bronze data we control end-to-end, so a bad line is a data-quality problem
    to investigate, not a record to skip (DESIGN.md §1: verified, not assumed).
    """
    topic = _CHANNEL_TOPIC[channel]
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
            t_receipt_ns = int(record["t_receipt"] * 1_000_000_000)
            key = _extract_key(record, channel)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SourceError(source_id, line_no, f"{type(exc).__name__}: {exc}") from exc
        yield SourceRecord(t_receipt_ns=t_receipt_ns, topic=topic, key=key, raw=stripped)


class LocalFileSource:
    """Reads a local bronze .jsonl.gz file."""

    def __init__(self, path: Path | str, channel: Channel) -> None:
        if channel not in _CHANNEL_TOPIC:
            raise ValueError(f"unknown channel: {channel!r}")
        self.path = Path(path)
        self.channel = channel

    @property
    def source_id(self) -> str:
        return str(self.path)

    def records(self) -> Iterator[SourceRecord]:
        with gzip.open(self.path, "rb") as f:
            yield from _parse_lines(iter(f), self.channel, self.source_id)


class S3FileSource:
    """Reads a bronze .jsonl.gz object from S3.

    The object body is buffered in memory before decompression — bronze_merged
    files are at most tens of MB, so this is simpler and more robust than
    streaming decompression over the network.
    """

    def __init__(self, bucket: str, key: str, channel: Channel, s3_client: Any = None) -> None:
        if channel not in _CHANNEL_TOPIC:
            raise ValueError(f"unknown channel: {channel!r}")
        self.bucket = bucket
        self.key = key
        self.channel = channel
        self._s3 = s3_client

    @property
    def source_id(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    def records(self) -> Iterator[SourceRecord]:
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        body = self._s3.get_object(Bucket=self.bucket, Key=self.key)["Body"].read()
        with gzip.open(io.BytesIO(body), "rb") as f:
            yield from _parse_lines(iter(f), self.channel, self.source_id)
