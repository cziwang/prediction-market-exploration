"""Merge small bronze files into one file per channel per date.

Bronze files are tiny (one per 60s flush). Merging them into one file
per channel per date makes build_silver fast (3 S3 GETs instead of 3,000).

Writes to bronze_merged/ — original bronze/ files are untouched.

Usage:
    python -m v2.scripts.merge_bronze --all
    python -m v2.scripts.merge_bronze --date 2026-04-19
    python -m v2.scripts.merge_bronze --all --dry-run
"""

from __future__ import annotations

import argparse
import gzip
import logging

import boto3

from v2.app.core.config import S3_BUCKET

log = logging.getLogger(__name__)

SOURCE = "kalshi_ws"
CHANNELS = ("orderbook_snapshot", "orderbook_delta", "trade")


def _discover_dates(s3, bucket: str) -> list[str]:
    """Find all dates with bronze data."""
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


def _list_keys(s3, bucket: str, channel: str, date: str) -> list[str]:
    y, m, d = date.split("-")
    prefix = f"bronze/{SOURCE}/{channel}/{y}/{m}/{d}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".jsonl.gz"):
                keys.append(obj["Key"])
    return keys


def merge_date(s3, bucket: str, date: str, *, dry_run: bool = False) -> None:
    """Merge all bronze files for one date into one file per channel."""
    for channel in CHANNELS:
        keys = _list_keys(s3, bucket, channel, date)
        if not keys:
            continue

        out_key = f"bronze_merged/{SOURCE}/{channel}/date={date}/merged.jsonl.gz"

        if dry_run:
            log.info("  [DRY RUN] %s: %d files → %s", channel, len(keys), out_key)
            continue

        # Concatenate all lines (no JSON parsing needed)
        all_lines: list[bytes] = []
        for key in keys:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            data = gzip.decompress(body)
            for line in data.split(b"\n"):
                if line.strip():
                    all_lines.append(line)

        # Compress and upload
        merged = gzip.compress(b"\n".join(all_lines) + b"\n")
        s3.put_object(Bucket=bucket, Key=out_key, Body=merged,
                      ContentType="application/gzip")
        log.info("  %s: %d files, %d records → %s (%.1f MB)",
                 channel, len(keys), len(all_lines), out_key,
                 len(merged) / 1024 / 1024)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Single date (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true", help="All dates with bronze data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    s3 = boto3.client("s3")

    if args.date:
        dates = [args.date]
    elif args.all:
        log.info("discovering dates...")
        dates = _discover_dates(s3, S3_BUCKET)
        log.info("found %d dates", len(dates))
    else:
        parser.error("provide --date or --all")
        return

    for date in dates:
        log.info("merging %s", date)
        merge_date(s3, S3_BUCKET, date, dry_run=args.dry_run)

    log.info("done — %d date(s)", len(dates))


if __name__ == "__main__":
    main()
