"""Fetch all NBA market metadata from Kalshi and write to S3 as Parquet.

Combines the live SDK (active + recently settled markets) with the
historical API (older settled markets) to build a complete reference
table of every NBA market ticker, its metadata, and settlement outcome.

Usage:
    python -m v2.scripts.fetch_markets                     # all NBA series
    python -m v2.scripts.fetch_markets --series KXNBAGAME  # single series
    python -m v2.scripts.fetch_markets --dry-run            # preview only
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import pyarrow as pa
import pyarrow.parquet as pq
import requests

from v2.app.core.config import S3_BUCKET

# All NBA series on Kalshi (superset of what the live ingester tracks).
ALL_NBA_SERIES = [
    # Game-level
    "KXNBAGAME",      # win/loss
    "KXNBASPREAD",    # point spread
    "KXNBATOTAL",     # total points over/under
    # Player props
    "KXNBAPTS",       # player points
    "KXNBAREB",       # player rebounds
    "KXNBAAST",       # player assists
    "KXNBA3PT",       # player threes
    "KXNBABLK",       # player blocks
    "KXNBASTL",       # player steals
    # Season / Playoff
    "KXNBA",          # NBA Finals winner
    "KXNBASERIES",    # playoff series winner
    "KXNBAPLAYOFF",   # playoff qualifier
    "KXNBAALLSTAR",   # all-star game
]

S3_KEY = "reference/kalshi_markets.parquet"

SCHEMA = pa.schema([
    ("ticker", pa.string()),
    ("series_ticker", pa.string()),
    ("event_ticker", pa.string()),
    ("title", pa.string()),
    ("yes_sub_title", pa.string()),
    ("no_sub_title", pa.string()),
    ("status", pa.string()),
    ("result", pa.string()),
    ("open_time", pa.timestamp("us", tz="UTC")),
    ("close_time", pa.timestamp("us", tz="UTC")),
    ("expiration_time", pa.timestamp("us", tz="UTC")),
    ("settlement_time", pa.timestamp("us", tz="UTC")),
    ("volume", pa.int64()),
    ("volume_24h", pa.int64()),
    ("last_price_cents", pa.int32()),
    ("settlement_value_cents", pa.int32()),
])


# ── Raw HTTP helpers (SDK has stale enum validation, so we use raw HTTP) ─

API_HOST = "https://api.elections.kalshi.com/trade-api/v2"


def _get(path: str, params: dict | None = None) -> dict:
    for attempt in range(4):
        resp = requests.get(f"{API_HOST}{path}", params=params)
        if resp.status_code == 429:
            wait = 2 ** (attempt + 1)
            print(f"  rate-limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


def _paginate_markets(series_ticker: str, status: str | None = None) -> Iterator[dict]:
    """Paginate the live /markets endpoint (raw HTTP, no SDK)."""
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 1000, "series_ticker": series_ticker}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        resp = _get("/markets", params)
        for m in resp.get("markets") or []:
            yield m
        cursor = resp.get("cursor")
        if not cursor:
            return


def _paginate_historical(series_ticker: str) -> Iterator[dict]:
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 1000, "series_ticker": series_ticker}
        if cursor:
            params["cursor"] = cursor
        resp = _get("/historical/markets", params)
        for m in resp.get("markets") or []:
            yield m
        cursor = resp.get("cursor")
        if not cursor:
            return


# ── Normalization ────────────────────────────────────────────────────

def _parse_ts(val: Any) -> datetime | None:
    """Parse a timestamp from the Kalshi API (ISO string or None)."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val
    s = str(val)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _cents(val: Any) -> int | None:
    """Convert Kalshi price (0-100 int or 4-decimal dollar float) to int cents."""
    if val is None:
        return None
    v = float(val)
    if v <= 1.0:
        return int(round(v * 100))
    return int(round(v))


def _fp_to_int(val: Any) -> int | None:
    """Convert Kalshi _fp string ('98064.36') to int."""
    if val is None:
        return None
    return int(round(float(val)))


def _normalize(m: dict, series_ticker: str) -> dict:
    """Normalize a Kalshi API market dict to a flat row."""
    return {
        "ticker": m.get("ticker"),
        "series_ticker": series_ticker,
        "event_ticker": m.get("event_ticker"),
        "title": m.get("title"),
        "yes_sub_title": m.get("yes_sub_title"),
        "no_sub_title": m.get("no_sub_title"),
        "status": m.get("status"),
        "result": m.get("result") or "",
        "open_time": _parse_ts(m.get("open_time")),
        "close_time": _parse_ts(m.get("close_time")),
        "expiration_time": _parse_ts(m.get("expiration_time")),
        "settlement_time": _parse_ts(m.get("settlement_ts")),
        "volume": _fp_to_int(m.get("volume_fp")),
        "volume_24h": _fp_to_int(m.get("volume_24h_fp")),
        "last_price_cents": _cents(m.get("last_price_dollars")),
        "settlement_value_cents": _cents(m.get("settlement_value_dollars")),
    }


# ── Main ─────────────────────────────────────────────────────────────

def fetch_all(series_list: list[str]) -> list[dict]:
    """Fetch markets from both live and historical API, deduplicate."""
    by_ticker: dict[str, dict] = {}

    # 1. Live API — gets active + recently settled
    for series in series_list:
        print(f"[Live] {series}...", end=" ", flush=True)
        count = 0
        for m in _paginate_markets(series):
            row = _normalize(m, series)
            if row["ticker"]:
                by_ticker[row["ticker"]] = row
                count += 1
        print(f"{count} markets")
        time.sleep(0.3)

    # 2. Historical API — gets older settled markets
    for series in series_list:
        print(f"[Historical] {series}...", end=" ", flush=True)
        count_new = 0
        for m in _paginate_historical(series):
            row = _normalize(m, series)
            ticker = row.get("ticker")
            if ticker and ticker not in by_ticker:
                by_ticker[ticker] = row
                count_new += 1
            elif ticker and ticker in by_ticker:
                existing = by_ticker[ticker]
                if not existing["result"] and row["result"]:
                    by_ticker[ticker] = row
        print(f"{count_new} new")
        time.sleep(0.3)

    return sorted(by_ticker.values(), key=lambda r: r.get("ticker", ""))


def to_table(rows: list[dict]) -> pa.Table:
    """Convert list of dicts to a PyArrow Table with explicit schema."""
    arrays = {}
    for field in SCHEMA:
        vals = [r.get(field.name) for r in rows]
        arrays[field.name] = vals
    return pa.table(arrays, schema=SCHEMA)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default=None,
                    help="Single series ticker (default: all NBA series)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch and print stats, don't write to S3")
    args = ap.parse_args()

    series_list = [args.series] if args.series else ALL_NBA_SERIES
    rows = fetch_all(series_list)

    print(f"\nTotal: {len(rows)} unique markets")

    # Stats
    by_status: dict[str, int] = {}
    by_series: dict[str, int] = {}
    settled_with_result = 0
    for r in rows:
        by_status[r["status"] or "unknown"] = by_status.get(r["status"] or "unknown", 0) + 1
        by_series[r["series_ticker"] or "unknown"] = by_series.get(r["series_ticker"] or "unknown", 0) + 1
        if r["result"] in ("yes", "no"):
            settled_with_result += 1

    print(f"  By status: {by_status}")
    print(f"  By series: {by_series}")
    print(f"  With settlement result: {settled_with_result}")

    if args.dry_run:
        print("\n(dry run — not writing to S3)")
        return

    table = to_table(rows)
    print(f"\nWriting {table.num_rows} rows to s3://{S3_BUCKET}/{S3_KEY}...")

    import boto3
    s3 = boto3.client("s3")
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf, compression="zstd")
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=S3_KEY,
        Body=buf.getvalue().to_pybytes(),
    )
    print(f"  Done: s3://{S3_BUCKET}/{S3_KEY}")


if __name__ == "__main__":
    main()
