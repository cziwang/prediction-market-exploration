"""Materialize bronze JSONL → Parquet for fast notebook access.

Reads raw gzip-JSONL from bronze/{source}/{channel}/, parses into flat
columns, and writes a single Parquet file per channel to:

    s3://{bucket}/materialized/{source}/{channel}.parquet

Notebooks read one Parquet file instead of thousands of gzipped JSONL files.

Usage:
    python -m scripts.materialize.bronze_to_parquet
    python -m scripts.materialize.bronze_to_parquet --channel orderbook_delta
    python -m scripts.materialize.bronze_to_parquet --channel orderbook_snapshot
    python -m scripts.materialize.bronze_to_parquet --channel trade
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import time

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from app.core.config import S3_BUCKET

log = logging.getLogger(__name__)

SOURCE = "kalshi_ws"

# Column extraction per channel type
def _parse_orderbook_delta(rec: dict) -> dict | None:
    msg = rec["frame"]["msg"]
    return {
        "t_receipt": rec["t_receipt"],
        "conn_id": rec.get("conn_id"),
        "sid": rec["frame"]["sid"],
        "seq": rec["frame"]["seq"],
        "ticker": msg["market_ticker"],
        "price": float(msg["price_dollars"]),
        "delta": float(msg["delta_fp"]),
        "side": msg["side"],
        "ts": msg.get("ts"),
    }


def _parse_orderbook_snapshot(rec: dict) -> dict | None:
    msg = rec["frame"]["msg"]
    if "yes_dollars_fp" not in msg or "no_dollars_fp" not in msg:
        return None  # skip control messages (ok, error)
    yes_levels = msg["yes_dollars_fp"]
    no_levels = msg["no_dollars_fp"]

    yes_prices = [float(p) for p, _ in yes_levels]
    no_prices = [float(p) for p, _ in no_levels]
    best_bid = max(yes_prices) if yes_prices else None
    best_no_bid = max(no_prices) if no_prices else None
    best_ask = 1.0 - best_no_bid if best_no_bid else None

    return {
        "t_receipt": rec["t_receipt"],
        "conn_id": rec.get("conn_id"),
        "sid": rec["frame"]["sid"],
        "seq": rec["frame"]["seq"],
        "ticker": msg["market_ticker"],
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": (best_ask - best_bid) if (best_ask is not None and best_bid is not None) else None,
        "yes_depth": sum(float(s) for _, s in yes_levels),
        "no_depth": sum(float(s) for _, s in no_levels),
        "n_yes_levels": len(yes_levels),
        "n_no_levels": len(no_levels),
        # Store full book as JSON strings for reconstruction
        "yes_book_json": json.dumps(yes_levels),
        "no_book_json": json.dumps(no_levels),
    }


def _parse_trade(rec: dict) -> dict | None:
    msg = rec["frame"]["msg"]
    if "yes_price_dollars" not in msg:
        return None
    return {
        "t_receipt": rec["t_receipt"],
        "conn_id": rec.get("conn_id"),
        "sid": rec["frame"]["sid"],
        "seq": rec["frame"]["seq"],
        "ticker": msg.get("market_ticker", ""),
        "yes_price": float(msg["yes_price_dollars"]),
        "no_price": float(msg.get("no_price_dollars", 0)),
        "count": int(msg.get("count", 0)),
        "taker_side": msg.get("taker_side", ""),
        "ts": msg.get("ts", ""),
    }


PARSERS = {
    "orderbook_delta": _parse_orderbook_delta,
    "orderbook_snapshot": _parse_orderbook_snapshot,
    "trade": _parse_trade,
}

CHANNELS = list(PARSERS.keys())


def materialize_channel(channel: str, bucket: str = S3_BUCKET) -> str:
    """Read all bronze files for a channel, parse, write Parquet to S3."""
    s3 = boto3.client("s3")
    parser = PARSERS[channel]

    # List all files
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=f"bronze/{SOURCE}/{channel}/"):
        keys.extend([o["Key"] for o in page.get("Contents", [])])

    log.info("materializing %s: %d files", channel, len(keys))

    # Parse all records
    rows = []
    skipped = 0
    for i, key in enumerate(keys):
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = gzip.decompress(obj["Body"].read()).decode()
        for line in data.strip().split("\n"):
            rec = json.loads(line)
            parsed = parser(rec)
            if parsed:
                rows.append(parsed)
            else:
                skipped += 1
        if (i + 1) % 100 == 0:
            log.info("  %d/%d files, %d rows so far", i + 1, len(keys), len(rows))

    log.info("parsed %d rows (%d skipped)", len(rows), skipped)
    if not rows:
        log.warning("no rows for %s — skipping", channel)
        return ""

    # Write Parquet
    table = pa.Table.from_pylist(rows)
    out_key = f"materialized/{SOURCE}/{channel}.parquet"

    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd")
    body = sink.getvalue().to_pybytes()

    s3.put_object(Bucket=bucket, Key=out_key, Body=body, ContentType="application/octet-stream")
    mb = len(body) / 1024 / 1024
    log.info("wrote s3://%s/%s (%d rows, %.1f MB)", bucket, out_key, len(rows), mb)
    return out_key


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Materialize bronze JSONL → Parquet")
    parser.add_argument("--channel", choices=CHANNELS, help="Single channel to materialize (default: all)")
    args = parser.parse_args()

    channels = [args.channel] if args.channel else CHANNELS
    t0 = time.time()
    for ch in channels:
        materialize_channel(ch)
    elapsed = time.time() - t0
    log.info("done in %.0fs", elapsed)


if __name__ == "__main__":
    main()
