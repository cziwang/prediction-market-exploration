"""One-time backfill: convert silver v=2 files to v=3 format.

For each date and event type with v=2 data:
1. Read all v=2 Parquet files
2. Convert t_receipt (float seconds) → t_receipt_ns (int64 nanoseconds)
3. Apply explicit v=3 schema with dictionary encoding
4. Sort by t_receipt_ns
5. Write a single compacted v=3 file

v=2 files are left in place for backward compatibility.

Usage:
    python -m v2.scripts.backfill_silver_v3
    python -m v2.scripts.backfill_silver_v3 --event-type OrderBookUpdate
    python -m v2.scripts.backfill_silver_v3 --dry-run
"""

from __future__ import annotations

import argparse
import io
import logging
import uuid

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from v2.app.core.config import S3_BUCKET, SILVER_VERSION
from v2.app.services.silver_writer import SCHEMAS

log = logging.getLogger(__name__)

SOURCE = "kalshi_ws"
V2_VERSION = 2


def discover_dates(s3, bucket: str, event_type: str) -> list[str]:
    """Find all date partitions with v=2 data for an event type."""
    prefix = f"silver/{SOURCE}/{event_type}/"
    paginator = s3.get_paginator("list_objects_v2")
    dates = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Extract date from .../date=YYYY-MM-DD/v=2/...
            if f"/v={V2_VERSION}/" in key:
                parts = key.split("/")
                for p in parts:
                    if p.startswith("date="):
                        dates.add(p[5:])
    return sorted(dates)


def backfill_partition(
    s3,
    bucket: str,
    event_type: str,
    date: str,
    dry_run: bool = False,
) -> dict | None:
    """Convert one v=2 partition to v=3."""
    v2_prefix = f"silver/{SOURCE}/{event_type}/date={date}/v={V2_VERSION}/"
    v3_prefix = f"silver/{SOURCE}/{event_type}/date={date}/v={SILVER_VERSION}/"

    # Check if v=3 already exists
    paginator = s3.get_paginator("list_objects_v2")
    v3_exists = False
    for page in paginator.paginate(Bucket=bucket, Prefix=v3_prefix, MaxKeys=1):
        if page.get("Contents"):
            v3_exists = True
    if v3_exists:
        log.info("skip %s %s — v=%d already exists", event_type, date, SILVER_VERSION)
        return None

    # Read all v=2 files
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
        log.info("DRY RUN %s %s: %d files, %d rows → would backfill to v=%d",
                 event_type, date, len(keys), total_rows, SILVER_VERSION)
        return {"event_type": event_type, "date": date, "rows": total_rows}

    # Convert t_receipt (float) → t_receipt_ns (int64)
    if "t_receipt" in merged.column_names:
        t_receipt = merged.column("t_receipt").to_pylist()
        t_receipt_ns = [int(t * 1_000_000_000) for t in t_receipt]
        merged = merged.drop("t_receipt")
        merged = merged.add_column(0, "t_receipt_ns", pa.array(t_receipt_ns, type=pa.int64()))

    # Sort by t_receipt_ns
    indices = merged.column("t_receipt_ns").to_pylist()
    sorted_idx = sorted(range(len(indices)), key=lambda i: indices[i])
    merged = merged.take(sorted_idx)

    # Cast to v=3 schema if available
    schema = SCHEMAS.get(event_type)
    if schema is not None:
        # Reorder columns to match schema and cast types
        arrays = []
        for field in schema:
            col = merged.column(field.name)
            if col.type != field.type:
                col = col.cast(field.type)
            arrays.append(col)
        merged = pa.Table.from_arrays(arrays, schema=schema)

    # Write
    sink = pa.BufferOutputStream()
    pq.write_table(merged, sink, compression="zstd", row_group_size=100_000)
    body = sink.getvalue().to_pybytes()

    v3_key = f"{v3_prefix}compacted-{uuid.uuid4().hex}.parquet"
    s3.put_object(Bucket=bucket, Key=v3_key, Body=body,
                  ContentType="application/octet-stream")

    log.info("backfilled %s %s: %d files → %s (%d rows, %d bytes)",
             event_type, date, len(keys), v3_key, total_rows, len(body))
    return {"event_type": event_type, "date": date, "rows": total_rows, "key": v3_key}


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Backfill silver v=2 to v=3")
    parser.add_argument("--event-type", help="Single event type. Omit for all.")
    parser.add_argument("--bucket", default=S3_BUCKET)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s3 = boto3.client("s3")
    event_types = [args.event_type] if args.event_type else list(SCHEMAS.keys())

    total_partitions = 0
    total_rows = 0

    for et in event_types:
        dates = discover_dates(s3, args.bucket, et)
        if not dates:
            log.info("no v=%d data for %s", V2_VERSION, et)
            continue
        log.info("found %d dates for %s", len(dates), et)

        for date in dates:
            result = backfill_partition(s3, args.bucket, et, date, args.dry_run)
            if result:
                total_partitions += 1
                total_rows += result["rows"]

    print(f"\nBackfilled {total_partitions} partitions, {total_rows} total rows.")


if __name__ == "__main__":
    main()
