"""
Merge fragmented Kalshi WS bronze files from S3 into one gzip-JSONL per channel per date.

Reads from: bronze/kalshi_ws/{channel}/YYYY/MM/DD/HH/{uuid}.jsonl.gz
Writes to:  bronze_merged/kalshi_ws/{channel}/date=YYYY-MM-DD/merged.jsonl.gz

Usage:
    python scripts/merge_kalshi_ws.py --date 2026-04-18
    python scripts/merge_kalshi_ws.py --start 2026-04-18 --end 2026-05-03
    python scripts/merge_kalshi_ws.py --start 2026-04-18 --end 2026-05-03 --channels orderbook_delta trade
"""

import argparse
import gzip
import io
import json
import sys
from datetime import date, timedelta

import boto3

BUCKET = "prediction-markets-data"
SRC_PREFIX = "bronze/kalshi_ws"
DST_PREFIX = "bronze_merged/kalshi_ws"
DEFAULT_CHANNELS = ["orderbook_delta", "orderbook_snapshot", "trade"]


def date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def list_keys_for_date(s3, channel: str, dt: date) -> list[str]:
    prefix = f"{SRC_PREFIX}/{channel}/{dt.strftime('%Y/%m/%d')}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def merge_channel_date(s3, channel: str, dt: date) -> int:
    keys = list_keys_for_date(s3, channel, dt)
    if not keys:
        return 0

    # Collect all records, then sort by t_receipt before writing.
    # S3 lists keys lexicographically — within an hour prefix that's random
    # UUID order, not time order, so concatenation alone produces a file with
    # ~60s flush chunks shuffled inside each hour.
    records: list[tuple[float, str]] = []
    for key in keys:
        try:
            resp = s3.get_object(Bucket=BUCKET, Key=key)
            with gzip.open(io.BytesIO(resp["Body"].read()), "rt") as f:
                for line in f:
                    if line.strip():
                        t_receipt = json.loads(line)["t_receipt"]
                        records.append((t_receipt, line))
        except Exception as e:
            print(f"  SKIP {key}: {e}", file=sys.stderr)

    if not records:
        return 0

    records.sort(key=lambda r: r[0])

    buf = io.BytesIO()
    n_records = len(records)
    with gzip.open(buf, "wt") as out:
        for _, line in records:
            out.write(line if line.endswith("\n") else line + "\n")

    dst_key = f"{DST_PREFIX}/{channel}/date={dt.isoformat()}/merged.jsonl.gz"
    s3.put_object(Bucket=BUCKET, Key=dst_key, Body=buf.getvalue())
    return n_records


def main():
    parser = argparse.ArgumentParser(description="Merge Kalshi WS bronze files by channel and date")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", help="Single date (YYYY-MM-DD)")
    group.add_argument("--start", help="Start date (YYYY-MM-DD), use with --end")
    parser.add_argument("--end", help="End date inclusive (YYYY-MM-DD), defaults to --start")
    parser.add_argument("--channels", nargs="+", default=DEFAULT_CHANNELS,
                        help=f"Channels to merge (default: {DEFAULT_CHANNELS})")
    args = parser.parse_args()

    if args.date:
        start = end = date.fromisoformat(args.date)
    else:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end or args.start)

    s3 = boto3.client("s3")

    for dt in date_range(start, end):
        for channel in args.channels:
            n = merge_channel_date(s3, channel, dt)
            if n > 0:
                print(f"{dt}  {channel:25s}  {n:>8,} records  →  {DST_PREFIX}/{channel}/date={dt}/merged.jsonl.gz")
            else:
                print(f"{dt}  {channel:25s}  (no data)")


if __name__ == "__main__":
    main()
