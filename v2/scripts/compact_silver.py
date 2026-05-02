"""Post-hoc compaction for silver Parquet files.

Reads all part-*.parquet files for a given date/event-type/version partition,
merges them into a single sorted file, verifies row counts, and deletes the
originals.

Safety: refuses to compact today's partition (live writer may be active).

Usage:
    python -m v2.scripts.compact_silver --event-type OrderBookUpdate --date 2026-04-30
    python -m v2.scripts.compact_silver --date 2026-04-30              # all event types
    python -m v2.scripts.compact_silver --date 2026-04-30 --dry-run    # preview only
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import uuid
from datetime import datetime, timezone

import boto3
import pyarrow.parquet as pq

from v2.app.core.config import S3_BUCKET, SILVER_VERSION
from v2.app.services.silver_writer import SCHEMAS

log = logging.getLogger(__name__)

SOURCE = "kalshi_ws"


def compact_partition(
    s3,
    bucket: str,
    event_type: str,
    date: str,
    version: int,
    dry_run: bool = False,
) -> dict | None:
    """Compact all part files in one partition into a single sorted file.

    Returns a summary dict or None if nothing to compact.
    """
    prefix = f"silver/{SOURCE}/{event_type}/date={date}/v={version}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            if k.endswith(".parquet"):
                keys.append(k)

    if len(keys) <= 1:
        log.info("skip %s — %d file(s), nothing to compact", prefix, len(keys))
        return None

    # Read all files
    tables = []
    total_input_bytes = 0
    for k in keys:
        resp = s3.get_object(Bucket=bucket, Key=k)
        body = resp["Body"].read()
        total_input_bytes += len(body)
        tables.append(pq.read_table(io.BytesIO(body)))

    import pyarrow as pa
    merged = pa.concat_tables(tables)
    total_rows = len(merged)

    # Sort by t_receipt_ns
    sort_col = "t_receipt_ns" if "t_receipt_ns" in merged.column_names else "t_receipt"
    indices = merged.column(sort_col).to_pylist()
    sorted_indices = sorted(range(len(indices)), key=lambda i: indices[i])
    merged = merged.take(sorted_indices)

    summary = {
        "event_type": event_type,
        "date": date,
        "input_files": len(keys),
        "total_rows": total_rows,
        "input_bytes": total_input_bytes,
    }

    if dry_run:
        log.info("DRY RUN %s: %d files, %d rows, %d bytes → would compact",
                 prefix, len(keys), total_rows, total_input_bytes)
        return summary

    # Write compacted file
    sink = pa.BufferOutputStream()
    pq.write_table(merged, sink, compression="zstd", row_group_size=100_000)
    compacted_bytes = sink.getvalue().to_pybytes()

    compacted_key = f"{prefix}compacted-{uuid.uuid4().hex}.parquet"
    s3.put_object(Bucket=bucket, Key=compacted_key, Body=compacted_bytes,
                  ContentType="application/octet-stream")

    # Verify row count by reading back
    resp = s3.get_object(Bucket=bucket, Key=compacted_key)
    verify_table = pq.read_table(io.BytesIO(resp["Body"].read()))
    if len(verify_table) != total_rows:
        log.error("ROW COUNT MISMATCH: expected %d, got %d — aborting delete",
                  total_rows, len(verify_table))
        return summary

    # Delete originals
    for k in keys:
        s3.delete_object(Bucket=bucket, Key=k)

    summary["output_bytes"] = len(compacted_bytes)
    summary["compacted_key"] = compacted_key
    ratio = total_input_bytes / len(compacted_bytes) if compacted_bytes else 0
    log.info("compacted %s: %d files (%d bytes) → 1 file (%d bytes, %.1fx), %d rows",
             prefix, len(keys), total_input_bytes, len(compacted_bytes), ratio, total_rows)
    return summary


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Compact silver Parquet partitions")
    parser.add_argument("--event-type", help="Event type (e.g. OrderBookUpdate). Omit for all.")
    parser.add_argument("--date", required=True, help="Date partition (YYYY-MM-DD)")
    parser.add_argument("--version", type=int, default=SILVER_VERSION)
    parser.add_argument("--bucket", default=S3_BUCKET)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.date >= today:
        log.error("refusing to compact today or future date %s (live writer may be active)", args.date)
        sys.exit(1)

    s3 = boto3.client("s3")
    event_types = [args.event_type] if args.event_type else list(SCHEMAS.keys())

    results = []
    for et in event_types:
        result = compact_partition(s3, args.bucket, et, args.date, args.version, args.dry_run)
        if result:
            results.append(result)

    if not results:
        print("Nothing to compact.")
    else:
        total_files = sum(r["input_files"] for r in results)
        total_rows = sum(r["total_rows"] for r in results)
        print(f"\nCompacted {total_files} files, {total_rows} total rows across {len(results)} partitions.")


if __name__ == "__main__":
    main()
