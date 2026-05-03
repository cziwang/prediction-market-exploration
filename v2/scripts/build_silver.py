"""Unified silver builder: derive all silver event types from bronze.

Streams bronze records through KalshiTransform and writes silver Parquet
incrementally. Reads from bronze_merged/ (one file per channel per date)
if available, falling back to bronze/ (many small files).

Memory-efficient: depth rows are flushed to Parquet in chunks of 100K rows
instead of accumulated in memory. Peak usage ~300-500 MB regardless of
date size.

Usage:
    python -m v2.scripts.build_silver --all --delete-existing
    python -m v2.scripts.build_silver --date 2026-04-26
    python -m v2.scripts.build_silver --start 2026-04-22 --end 2026-05-01
    python -m v2.scripts.build_silver --all --dry-run
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import tempfile
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from v2.app.core.config import S3_BUCKET, SILVER_VERSION
from v2.app.services.silver_writer import SCHEMAS, ROW_GROUP_SIZE
from v2.app.transforms.kalshi_ws import KalshiTransform

log = logging.getLogger(__name__)

SOURCE = "kalshi_ws"
CHANNELS = ("orderbook_snapshot", "orderbook_delta", "trade")
DEPTH_FLUSH_SIZE = 100_000  # flush depth rows to Parquet every N rows

# ── Validation ──────────────────────────────────────────────────────

VALIDATE_EVERY_N = 10_000       # check book invariants every N frames
MAX_CROSSED_RATE = 0.05         # abort if >5% of depth rows are crossed


class SilverValidationError(Exception):
    """Raised when silver validation fails — prevents writing bad data to S3."""
    pass


def _validate_books_inline(transform: KalshiTransform, frame_num: int) -> list[str]:
    """Check all books for negative sizes and out-of-range prices.
    Returns list of violation strings (empty = healthy)."""
    violations = []
    for ticker, book in transform._books.items():
        for price, size in book.yes_book.items():
            if size < 0:
                violations.append(
                    f"frame {frame_num}: {ticker} yes {price}c size={size}")
            if price < 1 or price > 99:
                violations.append(
                    f"frame {frame_num}: {ticker} yes price={price} out of range")
        for price, size in book.no_book.items():
            if size < 0:
                violations.append(
                    f"frame {frame_num}: {ticker} no {price}c size={size}")
            if price < 1 or price > 99:
                violations.append(
                    f"frame {frame_num}: {ticker} no price={price} out of range")
    return violations


def _validate_final(depth_path: str, trade_count: int,
                    bronze_trade_count: int) -> None:
    """Final validation before S3 upload. Raises SilverValidationError on failure."""
    table = pq.read_table(depth_path)

    # 1. Trade count must match
    if trade_count != bronze_trade_count:
        raise SilverValidationError(
            f"Trade count mismatch: {trade_count} events vs "
            f"{bronze_trade_count} bronze trade frames")

    # 2. Crossed book rate
    spreads = table.column("spread").to_pylist()
    valid_spreads = [s for s in spreads if s is not None]
    if valid_spreads:
        crossed = sum(1 for s in valid_spreads if s < 0)
        rate = crossed / len(valid_spreads)
        if rate > MAX_CROSSED_RATE:
            raise SilverValidationError(
                f"Crossed book rate {rate:.1%} exceeds {MAX_CROSSED_RATE:.0%} "
                f"threshold ({crossed:,}/{len(valid_spreads):,} depth rows)")
        log.info("  validation: crossed rate %.1f%% (%d/%d depth rows)",
                 rate * 100, crossed, len(valid_spreads))

    # 3. No negative sizes in depth columns
    for side in ("bid", "ask"):
        for i in range(1, 11):
            col = f"{side}_{i}_size"
            vals = table.column(col).to_pylist()
            negatives = sum(1 for v in vals if v is not None and v < 0)
            if negatives > 0:
                raise SilverValidationError(
                    f"{negatives} negative values in {col}")

    log.info("  validation: all checks passed")


def _prepare_rows(events: list) -> list[dict]:
    """Convert Event dataclasses to dicts with int64 ns timestamps."""
    rows = []
    for e in events:
        row = asdict(e)
        row["t_receipt_ns"] = int(row.pop("t_receipt") * 1_000_000_000)
        t_ex = row.pop("t_exchange", None)
        row["t_exchange_ns"] = int(t_ex * 1_000_000_000) if t_ex is not None else None
        rows.append(row)
    rows.sort(key=lambda r: r["t_receipt_ns"])
    return rows


# ── Bronze reading ───────────────────────────────────────────────────


def _stream_merged(s3, bucket: str, channel: str, date: str):
    """Yield parsed records from a merged bronze file."""
    key = f"bronze_merged/{SOURCE}/{channel}/date={date}/merged.jsonl.gz"
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
    except s3.exceptions.NoSuchKey:
        return
    data = gzip.decompress(resp["Body"].read()).decode()
    for line in data.split("\n"):
        if line.strip():
            yield json.loads(line)


def _stream_raw(s3, bucket: str, channel: str, date: str):
    """Yield parsed records from raw bronze files (many small files)."""
    y, m, d = date.split("-")
    prefix = f"bronze/{SOURCE}/{channel}/{y}/{m}/{d}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".jsonl.gz"):
                keys.append(obj["Key"])
    for key in keys:
        resp = s3.get_object(Bucket=bucket, Key=key)
        data = gzip.decompress(resp["Body"].read()).decode()
        for line in data.split("\n"):
            if line.strip():
                yield json.loads(line)


def _has_merged(s3, bucket: str, date: str) -> bool:
    """Check if merged bronze exists for a date (check one channel)."""
    key = f"bronze_merged/{SOURCE}/orderbook_delta/date={date}/merged.jsonl.gz"
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def _load_records(s3, bucket: str, date: str) -> list[dict]:
    """Load all bronze records for a date, preferring merged files."""
    use_merged = _has_merged(s3, bucket, date)
    source = "bronze_merged" if use_merged else "bronze (raw)"
    log.info("  reading from %s", source)

    records = []
    for channel in CHANNELS:
        stream = _stream_merged(s3, bucket, channel, date) if use_merged \
            else _stream_raw(s3, bucket, channel, date)
        before = len(records)
        for rec in stream:
            records.append(rec)
        log.info("  %s: %s records", channel, f"{len(records) - before:,}")

    records.sort(key=lambda r: r.get("t_receipt", 0.0))
    return records


# ── Silver writing ───────────────────────────────────────────────────


def _upload_parquet(s3, bucket: str, event_type: str, date: str,
                    local_path: str) -> str:
    """Upload a local Parquet file to S3 silver path."""
    key = (
        f"silver/{SOURCE}/{event_type}/date={date}/"
        f"v={SILVER_VERSION}/compacted-{uuid.uuid4().hex}.parquet"
    )
    with open(local_path, "rb") as f:
        s3.put_object(Bucket=bucket, Key=key, Body=f.read(),
                      ContentType="application/octet-stream")
    return key


def _delete_existing_silver(s3, bucket: str, date: str) -> int:
    """Delete all existing silver v=3 files for a date."""
    deleted = 0
    for event_type in SCHEMAS:
        prefix = f"silver/{SOURCE}/{event_type}/date={date}/v={SILVER_VERSION}/"
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        if keys:
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in keys]},
            )
            deleted += len(keys)
            log.info("  deleted %d existing files for %s", len(keys), event_type)
    return deleted


# ── Transform seeding ───────────────────────────────────────────────


def _seed_transform(s3, bucket: str, date: str, records: list[dict],
                    transform: KalshiTransform) -> int:
    """Seed transform with previous day's book state for cross-midnight continuity.

    When a WS connection spans midnight, date D starts with deltas whose
    snapshots were on date D-1. Without seeding, those deltas are silently
    dropped (no book state → no depth rows).

    Replays ALL previous-day records (snapshots + deltas) for the matching
    conn_id so the transform reaches the correct book state at midnight.
    Results are discarded — only the internal book state carries over.

    Returns the number of tickers seeded.
    """
    # Find the conn_id of the first delta in the already-loaded records
    target_conn_id = None
    for rec in records:
        if rec.get("frame", {}).get("type") == "orderbook_delta":
            target_conn_id = rec.get("conn_id")
            break
    if not target_conn_id:
        return 0

    prev = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    use_merged = _has_merged(s3, bucket, prev)

    # Load snapshots + deltas from the previous day for the target conn_id
    seed_records: list[dict] = []
    for channel in ("orderbook_snapshot", "orderbook_delta"):
        stream = _stream_merged(s3, bucket, channel, prev) if use_merged \
            else _stream_raw(s3, bucket, channel, prev)
        for rec in stream:
            if rec.get("conn_id") == target_conn_id:
                seed_records.append(rec)

    if not seed_records:
        return 0

    # Replay in chronological order — snapshots set state, deltas evolve it
    seed_records.sort(key=lambda r: r.get("t_receipt", 0.0))
    for rec in seed_records:
        transform(rec["frame"], rec["t_receipt"], conn_id=rec.get("conn_id"))

    tickers_seeded = len(transform._books)
    log.info("  seeded %d tickers from %s (%s records, conn %s…)",
             tickers_seeded, prev, f"{len(seed_records):,}", target_conn_id[:8])

    # Prune books that are already crossed or invalid after seeding.
    # These are typically settled markets from the previous day that
    # accumulated drift during long unsnapshot'd sessions. Without
    # pruning, every delta for these tickers emits a crossed depth row.
    pruned = []
    for ticker in list(transform._books):
        book = transform._books[ticker]
        spread = book.spread
        if spread is not None and spread < 0:
            pruned.append(ticker)
            del transform._books[ticker]
    if pruned:
        log.info("  pruned %d crossed books after seeding", len(pruned))

    return tickers_seeded - len(pruned)


# ── Main build logic ─────────────────────────────────────────────────


def _silver_exists(s3, bucket: str, date: str) -> bool:
    """Check if OrderBookDepth silver already exists for a date."""
    prefix = f"silver/{SOURCE}/OrderBookDepth/date={date}/v={SILVER_VERSION}/"
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return resp.get("KeyCount", 0) > 0


def build_date(s3, bucket: str, date: str, *, dry_run: bool = False,
               delete_existing: bool = False, merged_only: bool = False) -> None:
    """Build all silver types for one date from bronze.

    Skips dates that already have silver data unless delete_existing=True.
    Streams records through the transform and writes depth rows
    incrementally to a temp Parquet file (chunked writes). Events
    (TradeEvent, BookInvalidated) are small enough to buffer in memory.
    """
    log.info("building silver for %s", date)

    if _silver_exists(s3, bucket, date):
        if not delete_existing:
            log.info("  skipping — silver already exists (use --delete-existing to rebuild)")
            return

    # Skip if no merged bronze and --merged-only is set
    if merged_only and not _has_merged(s3, bucket, date):
        log.info("  skipping — no merged bronze (run merge_bronze first)")
        return

    # Always clean before writing — prevents duplicates from partial runs
    _delete_existing_silver(s3, bucket, date)

    records = _load_records(s3, bucket, date)
    if not records:
        log.info("  no bronze records for %s", date)
        return

    log.info("  %s total records", f"{len(records):,}")

    # Count bronze trades for validation
    bronze_trade_count = sum(
        1 for r in records
        if r.get("frame", {}).get("type") == "trade"
        and r.get("frame", {}).get("msg", {}).get("yes_price_dollars") is not None
    )

    # Replay through transform with chunked depth writes
    transform = KalshiTransform()
    _seed_transform(s3, bucket, date, records, transform)
    events_by_type: dict[str, list] = defaultdict(list)
    depth_schema = SCHEMAS["OrderBookDepth"]

    depth_buf: list[dict] = []
    depth_total = 0
    depth_tmpfile = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
    depth_tmpfile.close()
    depth_writer = pq.ParquetWriter(
        depth_tmpfile.name, depth_schema, compression="zstd",
    )
    all_violations: list[str] = []

    try:
        for frame_num, rec in enumerate(records):
            frame = rec.get("frame", {})
            t_receipt = rec.get("t_receipt", 0.0)
            conn_id = rec.get("conn_id")
            result = transform(frame, t_receipt, conn_id=conn_id)

            for event in result.events:
                events_by_type[type(event).__name__].append(event)

            depth_buf.extend(result.depth_rows)

            # Inline validation every N frames
            if frame_num > 0 and frame_num % VALIDATE_EVERY_N == 0:
                violations = _validate_books_inline(transform, frame_num)
                if violations:
                    all_violations.extend(violations)
                    depth_writer.close()
                    os.unlink(depth_tmpfile.name)
                    raise SilverValidationError(
                        f"Inline validation failed at frame {frame_num:,}: "
                        f"{violations[:5]}")

            # Flush depth chunk when buffer is large enough
            if len(depth_buf) >= DEPTH_FLUSH_SIZE:
                table = pa.Table.from_pylist(depth_buf, schema=depth_schema)
                depth_writer.write_table(table)
                depth_total += len(depth_buf)
                depth_buf.clear()

        # Flush remaining depth rows
        if depth_buf:
            table = pa.Table.from_pylist(depth_buf, schema=depth_schema)
            depth_writer.write_table(table)
            depth_total += len(depth_buf)
            depth_buf.clear()

        depth_writer.close()
    except SilverValidationError:
        raise
    except Exception:
        depth_writer.close()
        os.unlink(depth_tmpfile.name)
        raise

    # Stats
    for event_type, events in events_by_type.items():
        log.info("  %s: %s events", event_type, f"{len(events):,}")
    log.info("  OrderBookDepth: %s rows", f"{depth_total:,}")

    # Final validation before upload
    trade_event_count = len(events_by_type.get("TradeEvent", []))
    try:
        _validate_final(depth_tmpfile.name, trade_event_count, bronze_trade_count)
    except SilverValidationError as e:
        log.error("  VALIDATION FAILED — aborting upload: %s", e)
        os.unlink(depth_tmpfile.name)
        raise

    if dry_run:
        log.info("  [DRY RUN] — not writing to S3")
        os.unlink(depth_tmpfile.name)
        return

    # Write event types (small, buffered in memory)
    for event_type, events in events_by_type.items():
        if not events:
            continue
        rows = _prepare_rows(events)
        schema = SCHEMAS.get(event_type)
        table = pa.Table.from_pylist(rows, schema=schema) if schema else pa.Table.from_pylist(rows)
        tmp = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
        tmp.close()
        pq.write_table(table, tmp.name, compression="zstd", row_group_size=ROW_GROUP_SIZE)
        key = _upload_parquet(s3, bucket, event_type, date, tmp.name)
        os.unlink(tmp.name)
        log.info("  wrote %s: %d rows → %s", event_type, len(rows), key)

    # Upload depth Parquet
    if depth_total > 0:
        key = _upload_parquet(s3, bucket, "OrderBookDepth", date, depth_tmpfile.name)
        size_mb = os.path.getsize(depth_tmpfile.name) / 1024 / 1024
        log.info("  wrote OrderBookDepth: %d rows (%.1f MB) → %s",
                 depth_total, size_mb, key)
    os.unlink(depth_tmpfile.name)


# ── CLI ──────────────────────────────────────────────────────────────


def _date_range(start: str, end: str) -> list[str]:
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    dates = []
    while s <= e:
        dates.append(s.strftime("%Y-%m-%d"))
        s += timedelta(days=1)
    return dates


def _discover_bronze_dates(s3, bucket: str) -> list[str]:
    prefix = f"bronze/{SOURCE}/orderbook_delta/"
    paginator = s3.get_paginator("list_objects_v2")
    dates = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            year_prefix = cp["Prefix"]
            for page2 in paginator.paginate(Bucket=bucket, Prefix=year_prefix, Delimiter="/"):
                for cp2 in page2.get("CommonPrefixes", []):
                    month_prefix = cp2["Prefix"]
                    for page3 in paginator.paginate(Bucket=bucket, Prefix=month_prefix, Delimiter="/"):
                        for cp3 in page3.get("CommonPrefixes", []):
                            parts = cp3["Prefix"].rstrip("/").split("/")
                            if len(parts) >= 6:
                                dates.add(f"{parts[3]}-{parts[4]}-{parts[5]}")
    return sorted(dates)


def main():
    parser = argparse.ArgumentParser(
        description="Build silver from bronze",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", help="Single date (YYYY-MM-DD)")
    parser.add_argument("--start", help="Start date for range")
    parser.add_argument("--end", help="End date for range")
    parser.add_argument("--all", action="store_true",
                        help="Auto-discover all dates with bronze data")
    parser.add_argument("--delete-existing", action="store_true",
                        help="Delete existing silver before writing")
    parser.add_argument("--merged-only", action="store_true",
                        help="Only process dates with merged bronze (skip raw)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print stats only, don't write to S3")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    s3 = boto3.client("s3")

    if args.date:
        dates = [args.date]
    elif args.start and args.end:
        dates = _date_range(args.start, args.end)
    elif args.all:
        log.info("discovering dates from bronze...")
        dates = _discover_bronze_dates(s3, S3_BUCKET)
        log.info("found %d dates: %s → %s", len(dates),
                 dates[0] if dates else "?", dates[-1] if dates else "?")
    else:
        parser.error("provide --date, --start/--end, or --all")
        return

    failed_dates = []
    for date in dates:
        try:
            build_date(s3, S3_BUCKET, date, dry_run=args.dry_run,
                       delete_existing=args.delete_existing,
                       merged_only=args.merged_only)
        except SilverValidationError as e:
            log.error("SKIPPED %s — validation failed: %s", date, e)
            failed_dates.append(date)

    if failed_dates:
        log.error("FAILED dates: %s", failed_dates)
    log.info("done — %d date(s) processed, %d failed validation",
             len(dates), len(failed_dates))


if __name__ == "__main__":
    main()
