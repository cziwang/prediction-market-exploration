"""CLI for the historical event replayer.

Usage:
    # Replay the golden game fixture into local Kafka
    python -m pm.replay --source golden

    # Replay specific dates from S3 bronze_merged
    python -m pm.replay --source s3 --start 2026-04-18 --end 2026-04-19

    # Real-time pacing (integration testing of the live stack)
    python -m pm.replay --source golden --speed 1.0

    # Dry run: read + merge + count, no Kafka needed
    python -m pm.replay --source golden --dry-run
"""

import argparse
from datetime import date, timedelta
from pathlib import Path

from pm.replay.producer import InMemoryProducer, Producer
from pm.replay.replayer import EventReplayer
from pm.replay.sources import BronzeSource, LocalFileSource, S3FileSource

GOLDEN_DIR = Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "golden"
BUCKET = "prediction-markets-data"
S3_CHANNELS = ["trade", "orderbook_delta"]  # book snapshots not replayed yet


def golden_sources() -> list[BronzeSource]:
    return [
        LocalFileSource(GOLDEN_DIR / "nba_pbp_0042500121.jsonl.gz", "live_pbp"),
        LocalFileSource(GOLDEN_DIR / "kalshi_trades_0042500121.jsonl.gz", "trade"),
    ]


def s3_sources(start: date, end: date) -> list[BronzeSource]:
    import boto3

    s3 = boto3.client("s3")
    sources: list[BronzeSource] = []

    d = start
    while d <= end:
        for channel in S3_CHANNELS:
            key = f"bronze_merged/kalshi_ws/{channel}/date={d.isoformat()}/merged.jsonl.gz"
            sources.append(S3FileSource(BUCKET, key, channel, s3_client=s3))
        d += timedelta(days=1)

    # All PBP games (each file is one game; replayer merges by time, so games
    # outside [start, end] simply interleave where their timestamps fall)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix="bronze_merged/nba_cdn/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".jsonl.gz"):
                sources.append(S3FileSource(BUCKET, obj["Key"], "live_pbp", s3_client=s3))
    return sources


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay bronze data into Kafka")
    parser.add_argument("--source", choices=["golden", "s3"], required=True)
    parser.add_argument("--start", help="S3 start date YYYY-MM-DD")
    parser.add_argument("--end", help="S3 end date YYYY-MM-DD (default: --start)")
    parser.add_argument("--speed", type=float, default=0.0, help="0=flat out, 1=real time")
    parser.add_argument("--bootstrap-servers", default="localhost:9092")
    parser.add_argument(
        "--dry-run", action="store_true", help="read+merge+count without producing to Kafka"
    )
    args = parser.parse_args()

    if args.source == "golden":
        sources = golden_sources()
    else:
        if not args.start:
            parser.error("--source s3 requires --start")
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else start
        sources = s3_sources(start, end)

    producer: Producer
    if args.dry_run:
        producer = InMemoryProducer()
    else:
        from pm.replay.producer import KafkaProducer

        producer = KafkaProducer(args.bootstrap_servers)

    print(f"replaying {len(sources)} sources (speed={args.speed}, dry_run={args.dry_run})")
    stats = EventReplayer(producer).replay(sources, speed_multiplier=args.speed)
    print(stats.summary())


if __name__ == "__main__":
    main()
