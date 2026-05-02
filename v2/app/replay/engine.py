"""Replay engine: reconstruct full-depth order books from bronze data.

Reads orderbook_snapshot + orderbook_delta from bronze S3, replays them
in sequence order, and emits full-depth book snapshots at configurable
intervals.

Usage:
    engine = ReplayEngine(s3, bucket="prediction-markets-data")
    snapshots = engine.replay(
        date="2026-04-26",
        tickers={"KXNBAGAME-26APR25DENMIN-DEN"},  # None = all tickers
        snapshot_mode="every_n",
        snapshot_interval=100,
    )
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from v2.app.replay.book_state import ReplayBookState, _dollars_to_cents

log = logging.getLogger(__name__)

SOURCE = "kalshi_ws"


@dataclass
class BookSnapshot:
    """A point-in-time capture of one ticker's full book."""
    t_receipt_ns: int
    t_exchange_ns: int | None
    market_ticker: str
    seq: int
    sid: int
    yes_levels: list[tuple[int, int]]  # [(price_cents, size), ...] sorted
    no_levels: list[tuple[int, int]]
    best_bid: int | None
    best_ask: int | None
    bid_size: int
    ask_size: int


@dataclass
class ReplayStats:
    """Summary statistics from a replay run."""
    records_processed: int = 0
    snapshots_processed: int = 0
    deltas_processed: int = 0
    deltas_skipped_no_book: int = 0
    snapshots_emitted: int = 0
    seq_gaps: int = 0
    validation_errors: int = 0
    tickers_seen: int = 0


def _parse_ts(ts) -> float | None:
    """Parse Kalshi timestamp to float seconds. Handles epoch, ISO, None."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return ts / 1000.0 if ts > 1e12 else float(ts)
    if isinstance(ts, str):
        if not ts:
            return None
        try:
            val = float(ts)
            return val / 1000.0 if val > 1e12 else val
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, AttributeError):
            pass
    return None


class ReplayEngine:
    """Replays bronze order book data to reconstruct full-depth books.

    Args:
        s3: boto3 S3 client.
        bucket: S3 bucket name.
    """

    def __init__(self, s3, *, bucket: str = "prediction-markets-data") -> None:
        self._s3 = s3
        self._bucket = bucket

    def replay(
        self,
        date: str,
        *,
        tickers: set[str] | None = None,
        snapshot_mode: str = "every_n",
        snapshot_interval: int = 100,
        validate: bool = True,
    ) -> tuple[list[BookSnapshot], ReplayStats]:
        """Replay one date and return book snapshots.

        Args:
            date: "YYYY-MM-DD"
            tickers: if set, only track these tickers. None = all.
            snapshot_mode: "every_event" | "every_n"
                - every_event: emit a snapshot after every delta
                - every_n: emit after every N deltas per ticker
            snapshot_interval: N for every_n mode.
            validate: if True, run book validation after each delta.

        Returns:
            (list of BookSnapshot, ReplayStats)
        """
        stats = ReplayStats()

        # 1. Load all bronze records for this date
        records = self._load_bronze(date, tickers)
        stats.records_processed = len(records)

        # 2. Sort by (sid, seq) for correct ordering
        records.sort(key=lambda r: (
            r.get("frame", {}).get("sid", 0),
            r.get("frame", {}).get("seq", 0),
        ))

        # 3. Replay
        books: dict[str, ReplayBookState] = {}
        delta_counts: dict[str, int] = {}  # per-ticker delta count for every_n
        last_seq: dict[int, int] = {}  # sid → last seq for gap detection
        snapshots: list[BookSnapshot] = []

        for rec in records:
            frame = rec.get("frame", {})
            msg = frame.get("msg", {})
            msg_type = frame.get("type", "")
            sid = frame.get("sid", 0)
            seq = frame.get("seq", 0)
            ticker = msg.get("market_ticker", "")
            t_receipt = rec.get("t_receipt", 0.0)

            if not ticker:
                continue
            if tickers is not None and ticker not in tickers:
                continue

            # Gap detection — only meaningful when replaying all tickers
            # on a sid. When filtering to specific tickers, other tickers'
            # deltas occupy the "missing" seq numbers.
            if tickers is None:
                if sid in last_seq:
                    expected = last_seq[sid] + 1
                    if seq > expected:
                        stats.seq_gaps += 1
                        log.debug("seq gap sid=%d: expected=%d got=%d (missed %d)",
                                  sid, expected, seq, seq - expected)
                last_seq[sid] = seq

            if msg_type == "orderbook_snapshot":
                stats.snapshots_processed += 1
                book = ReplayBookState.from_snapshot(msg)
                book.seq = seq
                book.sid = sid
                books[ticker] = book
                delta_counts[ticker] = 0

            elif msg_type == "orderbook_delta":
                stats.deltas_processed += 1

                if ticker not in books:
                    # No snapshot yet — create empty book from first delta
                    books[ticker] = ReplayBookState()
                    books[ticker].sid = sid
                    delta_counts[ticker] = 0

                book = books[ticker]
                price_cents = _dollars_to_cents(msg["price_dollars"])
                delta = int(round(float(msg["delta_fp"])))
                side = msg["side"]

                book.apply_delta(price_cents, delta, side)
                book.seq = seq

                # Validation
                if validate:
                    errors = book.validate()
                    if errors:
                        stats.validation_errors += len(errors)
                        for e in errors:
                            log.debug("validation %s seq=%d: %s — %s",
                                      ticker, seq, e.check, e.detail)

                # Snapshot emission
                delta_counts[ticker] = delta_counts.get(ticker, 0) + 1
                emit = False
                if snapshot_mode == "every_event":
                    emit = True
                elif snapshot_mode == "every_n":
                    emit = delta_counts[ticker] >= snapshot_interval
                    if emit:
                        delta_counts[ticker] = 0

                if emit:
                    t_exchange = _parse_ts(msg.get("ts"))
                    t_receipt_ns = int(t_receipt * 1_000_000_000)
                    t_exchange_ns = int(t_exchange * 1_000_000_000) if t_exchange else None

                    snapshots.append(BookSnapshot(
                        t_receipt_ns=t_receipt_ns,
                        t_exchange_ns=t_exchange_ns,
                        market_ticker=ticker,
                        seq=seq,
                        sid=sid,
                        yes_levels=book.levels("yes"),
                        no_levels=book.levels("no"),
                        best_bid=book.best_bid,
                        best_ask=book.best_ask,
                        bid_size=book.bid_size,
                        ask_size=book.ask_size,
                    ))
                    stats.snapshots_emitted += 1

        stats.tickers_seen = len(books)
        return snapshots, stats

    def _load_bronze(
        self, date: str, tickers: set[str] | None,
    ) -> list[dict]:
        """Load orderbook_snapshot + orderbook_delta records for a date."""
        records = []
        for channel in ("orderbook_snapshot", "orderbook_delta"):
            keys = self._list_keys(channel, date)
            log.info("loading %s %s: %d files", channel, date, len(keys))
            for key in keys:
                resp = self._s3.get_object(Bucket=self._bucket, Key=key)
                data = gzip.decompress(resp["Body"].read()).decode()
                for line in data.strip().split("\n"):
                    if not line:
                        continue
                    rec = json.loads(line)
                    # Early filter by ticker to save memory
                    if tickers is not None:
                        msg = rec.get("frame", {}).get("msg", {})
                        ticker = msg.get("market_ticker", "")
                        if ticker and ticker not in tickers:
                            continue
                    records.append(rec)
        return records

    def _list_keys(self, channel: str, date: str) -> list[str]:
        """List bronze S3 keys for a channel on a date."""
        y, m, d = date.split("-")
        prefix = f"bronze/{SOURCE}/{channel}/{y}/{m}/{d}/"
        paginator = self._s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".jsonl.gz"):
                    keys.append(obj["Key"])
        return keys
