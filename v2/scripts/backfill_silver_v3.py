"""One-time backfill: rebuild silver v=3 from source data.

For OrderBookUpdate and TradeEvent: re-derives from bronze (raw WS frames)
because v=2 silver never captured t_exchange, sid, or seq.

For MM event types (MMQuoteEvent, MMFillEvent, etc.): converts from v=2
silver (these events don't have the new fields).

Usage:
    python -m v2.scripts.backfill_silver_v3
    python -m v2.scripts.backfill_silver_v3 --event-type OrderBookUpdate
    python -m v2.scripts.backfill_silver_v3 --dry-run
    python -m v2.scripts.backfill_silver_v3 --delete-existing   # re-derive even if v=3 exists
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import asdict

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from v2.app.core.config import S3_BUCKET, SILVER_VERSION
from v2.app.services.silver_writer import SCHEMAS, _prepare_rows
from v2.app.transforms.kalshi_ws import KalshiTransform

log = logging.getLogger(__name__)

SOURCE = "kalshi_ws"
V2_VERSION = 2

# Event types that need bronze re-derivation (have t_exchange/sid/seq)
BRONZE_EVENT_TYPES = {"OrderBookUpdate", "TradeEvent", "BookInvalidated"}


# ---------------------------------------------------------------------------
# Bronze-based backfill (OrderBookUpdate, TradeEvent)
# ---------------------------------------------------------------------------

def _discover_bronze_dates(s3, bucket: str) -> list[str]:
    """Find all dates with bronze data."""
    dates = set()
    for channel in ("orderbook_delta", "orderbook_snapshot", "trade"):
        prefix = f"bronze/{SOURCE}/{channel}/"
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                # prefix looks like bronze/kalshi_ws/channel/YYYY/
                year_prefix = cp["Prefix"]
                for page2 in paginator.paginate(Bucket=bucket, Prefix=year_prefix, Delimiter="/"):
                    for cp2 in page2.get("CommonPrefixes", []):
                        month_prefix = cp2["Prefix"]
                        for page3 in paginator.paginate(Bucket=bucket, Prefix=month_prefix, Delimiter="/"):
                            for cp3 in page3.get("CommonPrefixes", []):
                                # .../YYYY/MM/DD/
                                parts = cp3["Prefix"].rstrip("/").split("/")
                                if len(parts) >= 3:
                                    y, m, d = parts[-3], parts[-2], parts[-1]
                                    try:
                                        dates.add(f"{y}-{m}-{d}")
                                    except (ValueError, IndexError):
                                        pass
    return sorted(dates)


def _list_bronze_keys(s3, bucket: str, channel: str, date: str) -> list[str]:
    """List all bronze files for a channel on a specific date."""
    # date is YYYY-MM-DD, bronze path is .../YYYY/MM/DD/HH/
    y, m, d = date.split("-")
    prefix = f"bronze/{SOURCE}/{channel}/{y}/{m}/{d}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".jsonl.gz"):
                keys.append(obj["Key"])
    return keys


def backfill_from_bronze(
    s3,
    bucket: str,
    date: str,
    dry_run: bool = False,
) -> list[dict]:
    """Re-derive OrderBookUpdate + TradeEvent from bronze for one date."""
    results = []

    # Collect all bronze keys for this date
    channel_keys: dict[str, list[str]] = {}
    for channel in ("orderbook_snapshot", "orderbook_delta", "trade"):
        keys = _list_bronze_keys(s3, bucket, channel, date)
        if keys:
            channel_keys[channel] = keys

    if not channel_keys:
        return results

    total_files = sum(len(v) for v in channel_keys.values())
    if dry_run:
        log.info("DRY RUN bronze %s: %d files across %s",
                 date, total_files, list(channel_keys.keys()))
        return [{"event_type": "bronze", "date": date, "rows": 0}]

    # Read all bronze records, sorted by t_receipt
    records = []
    for channel, keys in channel_keys.items():
        for k in keys:
            resp = s3.get_object(Bucket=bucket, Key=k)
            data = gzip.decompress(resp["Body"].read()).decode()
            for line in data.strip().split("\n"):
                if not line:
                    continue
                rec = json.loads(line)
                records.append(rec)

    records.sort(key=lambda r: r.get("t_receipt", 0))

    # Replay through the transform
    transform = KalshiTransform()
    events_by_type: dict[str, list] = defaultdict(list)

    for rec in records:
        frame = rec.get("frame", {})
        t_receipt = rec.get("t_receipt", 0.0)
        conn_id = rec.get("conn_id")
        events = transform(frame, t_receipt, conn_id=conn_id)
        for event in events:
            events_by_type[type(event).__name__].append(event)

    # Write each event type to v=3
    for event_type, events in events_by_type.items():
        if not events:
            continue

        schema = SCHEMAS.get(event_type)
        rows = _prepare_rows(events)

        if schema is not None:
            table = pa.Table.from_pylist(rows, schema=schema)
        else:
            table = pa.Table.from_pylist(rows)

        sink = pa.BufferOutputStream()
        pq.write_table(table, sink, compression="zstd", row_group_size=100_000)
        body = sink.getvalue().to_pybytes()

        v3_key = (
            f"silver/{SOURCE}/{event_type}/date={date}/"
            f"v={SILVER_VERSION}/compacted-{uuid.uuid4().hex}.parquet"
        )
        s3.put_object(Bucket=bucket, Key=v3_key, Body=body,
                      ContentType="application/octet-stream")

        log.info("backfilled (bronze) %s %s: %d events → %s (%d bytes)",
                 event_type, date, len(events), v3_key, len(body))
        results.append({
            "event_type": event_type, "date": date,
            "rows": len(events), "key": v3_key,
        })

    return results


# ---------------------------------------------------------------------------
# Silver v=2 based backfill (MM event types)
# ---------------------------------------------------------------------------

def discover_v2_dates(s3, bucket: str, event_type: str) -> list[str]:
    """Find all date partitions with v=2 data for an event type."""
    prefix = f"silver/{SOURCE}/{event_type}/"
    paginator = s3.get_paginator("list_objects_v2")
    dates = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if f"/v={V2_VERSION}/" in key:
                parts = key.split("/")
                for p in parts:
                    if p.startswith("date="):
                        dates.add(p[5:])
    return sorted(dates)


def backfill_from_silver_v2(
    s3,
    bucket: str,
    event_type: str,
    date: str,
    dry_run: bool = False,
) -> dict | None:
    """Convert one v=2 silver partition to v=3 (for MM event types)."""
    v2_prefix = f"silver/{SOURCE}/{event_type}/date={date}/v={V2_VERSION}/"

    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=v2_prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])

    if not keys:
        return None

    tables = []
    for k in keys:
        resp = s3.get_object(Bucket=bucket, Key=k)
        tables.append(pq.read_table(io.BytesIO(resp["Body"].read())))

    merged = pa.concat_tables(tables, promote_options="permissive")
    total_rows = len(merged)

    if dry_run:
        log.info("DRY RUN silver %s %s: %d files, %d rows",
                 event_type, date, len(keys), total_rows)
        return {"event_type": event_type, "date": date, "rows": total_rows}

    # Convert t_receipt → t_receipt_ns
    if "t_receipt" in merged.column_names:
        t_receipt = merged.column("t_receipt").to_pylist()
        t_receipt_ns = [int(t * 1_000_000_000) for t in t_receipt]
        merged = merged.drop("t_receipt")
        merged = merged.add_column(0, "t_receipt_ns", pa.array(t_receipt_ns, type=pa.int64()))

    # Sort
    indices = merged.column("t_receipt_ns").to_pylist()
    sorted_idx = sorted(range(len(indices)), key=lambda i: indices[i])
    merged = merged.take(sorted_idx)

    # Cast to v=3 schema
    schema = SCHEMAS.get(event_type)
    if schema is not None:
        arrays = []
        for field in schema:
            if field.name in merged.column_names:
                col = merged.column(field.name)
                if col.type != field.type:
                    col = col.cast(field.type)
            else:
                # New field not in v=2 — fill with nulls
                col = pa.array([None] * total_rows, type=field.type)
            arrays.append(col)
        merged = pa.Table.from_arrays(arrays, schema=schema)

    sink = pa.BufferOutputStream()
    pq.write_table(merged, sink, compression="zstd", row_group_size=100_000)
    body = sink.getvalue().to_pybytes()

    v3_key = (
        f"silver/{SOURCE}/{event_type}/date={date}/"
        f"v={SILVER_VERSION}/compacted-{uuid.uuid4().hex}.parquet"
    )
    s3.put_object(Bucket=bucket, Key=v3_key, Body=body,
                  ContentType="application/octet-stream")

    log.info("backfilled (silver) %s %s: %d files → %s (%d rows, %d bytes)",
             event_type, date, len(keys), v3_key, total_rows, len(body))
    return {"event_type": event_type, "date": date, "rows": total_rows, "key": v3_key}


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def delete_v3_partition(s3, bucket: str, event_type: str, date: str) -> int:
    """Delete all v=3 files for a partition. Returns count deleted."""
    prefix = f"silver/{SOURCE}/{event_type}/date={date}/v={SILVER_VERSION}/"
    paginator = s3.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=bucket, Key=obj["Key"])
            deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Backfill silver v=3 from bronze + v=2")
    parser.add_argument("--event-type", help="Single event type. Omit for all.")
    parser.add_argument("--bucket", default=S3_BUCKET)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delete-existing", action="store_true",
                        help="Delete existing v=3 data before re-deriving")
    args = parser.parse_args()

    s3 = boto3.client("s3")
    total_partitions = 0
    total_rows = 0

    if args.event_type:
        event_types = [args.event_type]
    else:
        event_types = list(SCHEMAS.keys())

    # Phase 1: Bronze-based backfill for OrderBookUpdate/TradeEvent/BookInvalidated
    bronze_types = [et for et in event_types if et in BRONZE_EVENT_TYPES]
    if bronze_types:
        dates = _discover_bronze_dates(s3, args.bucket)
        if dates:
            log.info("found %d dates in bronze for %s", len(dates), bronze_types)
            for date in dates:
                # Check/delete existing v=3
                if args.delete_existing:
                    for et in bronze_types:
                        n = delete_v3_partition(s3, args.bucket, et, date)
                        if n:
                            log.info("deleted %d existing v=3 files for %s %s", n, et, date)
                else:
                    # Skip if any bronze event type already has v=3
                    v3_prefix = f"silver/{SOURCE}/OrderBookUpdate/date={date}/v={SILVER_VERSION}/"
                    resp = s3.list_objects_v2(Bucket=args.bucket, Prefix=v3_prefix, MaxKeys=1)
                    if resp.get("Contents"):
                        log.info("skip bronze %s — v=3 already exists", date)
                        continue

                results = backfill_from_bronze(s3, args.bucket, date, args.dry_run)
                for r in results:
                    total_partitions += 1
                    total_rows += r.get("rows", 0)
        else:
            log.info("no bronze data found")

    # Phase 2: Silver v=2 based backfill for MM event types
    silver_types = [et for et in event_types if et not in BRONZE_EVENT_TYPES]
    for et in silver_types:
        dates = discover_v2_dates(s3, args.bucket, et)
        if not dates:
            log.info("no v=%d data for %s", V2_VERSION, et)
            continue
        log.info("found %d dates for %s", len(dates), et)

        for date in dates:
            if args.delete_existing:
                n = delete_v3_partition(s3, args.bucket, et, date)
                if n:
                    log.info("deleted %d existing v=3 files for %s %s", n, et, date)
            else:
                v3_prefix = f"silver/{SOURCE}/{et}/date={date}/v={SILVER_VERSION}/"
                resp = s3.list_objects_v2(Bucket=args.bucket, Prefix=v3_prefix, MaxKeys=1)
                if resp.get("Contents"):
                    log.info("skip %s %s — v=3 already exists", et, date)
                    continue

            result = backfill_from_silver_v2(s3, args.bucket, et, date, args.dry_run)
            if result:
                total_partitions += 1
                total_rows += result["rows"]

    print(f"\nBackfilled {total_partitions} partitions, {total_rows} total rows.")


if __name__ == "__main__":
    main()
