"""
Merge fragmented NBA live_pbp bronze files from S3 into one gzip-JSONL per game.

Each S3 file contains a few actions from one poll. This script:
  1. Lists all files under bronze/nba_cdn/live_pbp/ for a given date range
  2. Downloads and parses each file
  3. Deduplicates by (game_id, action_number) — keeps earliest t_receipt
  4. Writes one sorted merged.jsonl.gz per game_id to the output directory

Usage:
    python scripts/merge_nba_pbp.py --date 2026-04-18
    python scripts/merge_nba_pbp.py --start 2026-04-18 --end 2026-05-03
    python scripts/merge_nba_pbp.py --date 2026-04-18 --out data/pbp/
"""

import argparse
import gzip
import io
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import boto3

BUCKET = "prediction-markets-data"
PREFIX = "bronze/nba_cdn/live_pbp"


def date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def list_keys_for_date(s3, dt: date) -> list[str]:
    prefix = f"{PREFIX}/{dt.strftime('%Y/%m/%d')}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def read_jsonl_gz(s3, key: str) -> list[dict]:
    resp = s3.get_object(Bucket=BUCKET, Key=key)
    with gzip.open(io.BytesIO(resp["Body"].read()), "rt") as f:
        return [json.loads(line) for line in f if line.strip()]


def merge_dates(start: date, end: date, out_dir: Path):
    s3 = boto3.client("s3")
    out_dir.mkdir(parents=True, exist_ok=True)

    # game_id -> {action_number -> record}
    games: dict[str, dict[int, dict]] = defaultdict(dict)

    for dt in date_range(start, end):
        keys = list_keys_for_date(s3, dt)
        print(f"{dt}  {len(keys)} files", flush=True)
        for key in keys:
            try:
                records = read_jsonl_gz(s3, key)
            except Exception as e:
                print(f"  SKIP {key}: {e}", file=sys.stderr)
                continue
            for rec in records:
                game_id = rec.get("game_id")
                action_number = rec.get("action_number")
                if game_id is None or action_number is None:
                    continue
                existing = games[game_id].get(action_number)
                # Keep earliest receipt
                if existing is None or rec["t_receipt"] < existing["t_receipt"]:
                    games[game_id][action_number] = rec

    print(f"\nGames found: {len(games)}")
    for game_id, actions in sorted(games.items()):
        # Sort by RECEIPT time, not action_number: the CDN can deliver edits
        # to earlier actions long after the fact (observed: a 94-minute-late
        # correction), so action order != receipt order. The replayer and all
        # receipt-view semantics require non-decreasing t_receipt within a
        # file. action_number breaks ties within the same poll response.
        sorted_records = sorted(actions.values(), key=lambda r: (r["t_receipt"], r["action_number"]))
        out_path = out_dir / f"nba_pbp_{game_id}.jsonl.gz"
        with gzip.open(out_path, "wt") as f:
            for rec in sorted_records:
                f.write(json.dumps(rec) + "\n")
        print(f"  {game_id}  {len(sorted_records)} actions  →  {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Merge NBA live_pbp bronze files by game")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", help="Single date (YYYY-MM-DD)")
    group.add_argument("--start", help="Start date (YYYY-MM-DD), use with --end")
    parser.add_argument("--end", help="End date inclusive (YYYY-MM-DD), defaults to --start")
    parser.add_argument("--out", default="data/pbp", help="Output directory (default: data/pbp)")
    args = parser.parse_args()

    if args.date:
        start = end = date.fromisoformat(args.date)
    else:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end or args.start)

    merge_dates(start, end, Path(args.out))


if __name__ == "__main__":
    main()
