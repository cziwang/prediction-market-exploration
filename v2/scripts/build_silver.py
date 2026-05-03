"""Unified silver builder: derive all silver event types from bronze.

Reads bronze data for a date, replays through KalshiTransform (which
produces TradeEvent + BookInvalidated + OrderBookDepth), and writes
each type to silver v=3 on S3.

Replaces both backfill_silver_v3.py and replay/build_depth.py.

Usage:
    # All types for one date
    python -m v2.scripts.build_silver --date 2026-04-26

    # Date range
    python -m v2.scripts.build_silver --start 2026-04-22 --end 2026-05-01

    # Single ticker
    python -m v2.scripts.build_silver --date 2026-04-26 --ticker KXNBAGAME-26APR25DENMIN-DEN

    # Dry run
    python -m v2.scripts.build_silver --date 2026-04-26 --dry-run

    # Skip dates that already have v=3 data
    python -m v2.scripts.build_silver --start 2026-04-22 --end 2026-05-01 --skip-existing
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
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


def _list_bronze_keys(s3, bucket: str, channel: str, date: str) -> list[str]:
    """List bronze S3 keys for a channel on a date."""
    y, m, d = date.split("-")
    prefix = f"bronze/{SOURCE}/{channel}/{y}/{m}/{d}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".jsonl.gz"):
                keys.append(obj["Key"])
    return keys


def _load_bronze(s3, bucket: str, date: str) -> list[dict]:
    """Load all bronze records (snapshot + delta + trade) for a date."""
    records = []
    for channel in ("orderbook_snapshot", "orderbook_delta", "trade"):
        keys = _list_bronze_keys(s3, bucket, channel, date)
        log.info("  %s: %d files", channel, len(keys))
        for key in keys:
            resp = s3.get_object(Bucket=bucket, Key=key)
            data = gzip.decompress(resp["Body"].read()).decode()
            for line in data.strip().split("\n"):
                if not line:
                    continue
                records.append(json.loads(line))
    return records


def _write_parquet(s3, bucket: str, event_type: str, date: str,
                   rows: list[dict]) -> str:
    """Write rows to silver Parquet on S3. Returns the S3 key."""
    schema = SCHEMAS.get(event_type)
    if schema is not None:
        table = pa.Table.from_pylist(rows, schema=schema)
    else:
        table = pa.Table.from_pylist(rows)

    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd", row_group_size=ROW_GROUP_SIZE)
    body = sink.getvalue().to_pybytes()

    key = (
        f"silver/{SOURCE}/{event_type}/date={date}/"
        f"v={SILVER_VERSION}/compacted-{uuid.uuid4().hex}.parquet"
    )
    s3.put_object(Bucket=bucket, Key=key, Body=body,
                  ContentType="application/octet-stream")
    return key


def _delete_existing_silver(s3, bucket: str, date: str) -> int:
    """Delete all existing silver v=3 files for a date. Returns count deleted."""
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
            log.info("  deleted %d existing files for %s/%s", len(keys), event_type, date)
    return deleted


def build_date(s3, bucket: str, date: str, *, dry_run: bool = False,
               delete_existing: bool = False) -> None:
    """Build all silver types for one date from bronze."""
    log.info("building silver for %s", date)

    records = _load_bronze(s3, bucket, date)
    if not records:
        log.info("  no bronze records for %s", date)
        return

    records.sort(key=lambda r: r.get("t_receipt", 0.0))
    log.info("  %s records loaded", f"{len(records):,}")

    # Replay through transform
    transform = KalshiTransform()
    events_by_type: dict[str, list] = defaultdict(list)
    depth_rows: list[dict] = []

    for rec in records:
        frame = rec.get("frame", {})
        t_receipt = rec.get("t_receipt", 0.0)
        conn_id = rec.get("conn_id")
        result = transform(frame, t_receipt, conn_id=conn_id)
        for event in result.events:
            events_by_type[type(event).__name__].append(event)
        depth_rows.extend(result.depth_rows)

    # Stats
    for event_type, events in events_by_type.items():
        log.info("  %s: %s events", event_type, f"{len(events):,}")
    log.info("  OrderBookDepth: %s rows", f"{len(depth_rows):,}")

    if dry_run:
        log.info("  [DRY RUN] — not writing to S3")
        return

    if delete_existing:
        _delete_existing_silver(s3, bucket, date)

    # Write event types (TradeEvent, BookInvalidated)
    for event_type, events in events_by_type.items():
        if not events:
            continue
        rows = _prepare_rows(events)
        key = _write_parquet(s3, bucket, event_type, date, rows)
        log.info("  wrote %s: %d rows → %s", event_type, len(rows), key)

    # Write depth rows (already in ns format)
    if depth_rows:
        depth_rows.sort(key=lambda r: r["t_receipt_ns"])
        key = _write_parquet(s3, bucket, "OrderBookDepth", date, depth_rows)
        log.info("  wrote OrderBookDepth: %d rows → %s", len(depth_rows), key)


def _date_range(start: str, end: str) -> list[str]:
    """Generate list of date strings from start to end (inclusive)."""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    dates = []
    while s <= e:
        dates.append(s.strftime("%Y-%m-%d"))
        s += timedelta(days=1)
    return dates


def _discover_bronze_dates(s3, bucket: str) -> list[str]:
    """Find all dates with bronze data by listing S3 prefixes."""
    prefix = f"bronze/{SOURCE}/orderbook_delta/"
    paginator = s3.get_paginator("list_objects_v2")
    dates = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            # prefix looks like bronze/kalshi_ws/orderbook_delta/2026/
            year_prefix = cp["Prefix"]
            for page2 in paginator.paginate(Bucket=bucket, Prefix=year_prefix, Delimiter="/"):
                for cp2 in page2.get("CommonPrefixes", []):
                    month_prefix = cp2["Prefix"]
                    for page3 in paginator.paginate(Bucket=bucket, Prefix=month_prefix, Delimiter="/"):
                        for cp3 in page3.get("CommonPrefixes", []):
                            # bronze/kalshi_ws/orderbook_delta/2026/04/26/
                            parts = cp3["Prefix"].rstrip("/").split("/")
                            if len(parts) >= 6:
                                dates.add(f"{parts[3]}-{parts[4]}-{parts[5]}")
    return sorted(dates)


def main():
    parser = argparse.ArgumentParser(
        description="Build silver from bronze (unified backfill + depth builder)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", help="Single date (YYYY-MM-DD)")
    parser.add_argument("--start", help="Start date for range (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date for range (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true",
                        help="Auto-discover all dates with bronze data")
    parser.add_argument("--delete-existing", action="store_true",
                        help="Delete existing silver for each date before writing")
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

    for date in dates:
        build_date(s3, S3_BUCKET, date, dry_run=args.dry_run,
                   delete_existing=args.delete_existing)

    log.info("done — %d date(s) processed", len(dates))


if __name__ == "__main__":
    main()
