"""Batch LOB reconstruction: snapshot-anchored windowing over bronze Parquet.

Reads bronze deltas + snapshots from S3, reconstructs full book state at
every tick, writes silver LOB Parquet back to S3.

Algorithm (from Marriott 2026):
    For each market m with ordered snapshots S1..Sn:
      For each window [ts(S_i), ts(S_{i+1})):
        1. Initialize book from S_i
        2. Replay deltas in timestamp order
        3. B_{t+1}(s, p) = max(0, B_t(s, p) + delta)
        4. Emit full book state after each delta group (same timestamp)
      Verify: reconstructed state at window end matches S_{i+1}
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import boto3
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

S3_BUCKET = "prediction-markets-data"
BRONZE_PREFIX = "mm/bronze"
SILVER_PREFIX = "mm/silver"


@dataclass
class ReconstructConfig:
    bucket: str = S3_BUCKET
    bronze_prefix: str = BRONZE_PREFIX
    silver_prefix: str = SILVER_PREFIX


# -- S3 helpers ----------------------------------------------------------


def _list_parquet_keys(s3, bucket: str, prefix: str) -> list[str]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])
    return keys


def _read_parquet_from_s3(s3, bucket: str, key: str) -> pl.DataFrame:
    resp = s3.get_object(Bucket=bucket, Key=key)
    buf = io.BytesIO(resp["Body"].read())
    return pl.read_parquet(buf)


def _load_all_parquet(s3, bucket: str, prefix: str) -> pl.DataFrame:
    keys = _list_parquet_keys(s3, bucket, prefix)
    if not keys:
        return pl.DataFrame()
    frames = [_read_parquet_from_s3(s3, bucket, k) for k in keys]
    return pl.concat(frames)


# -- Book state (dict-based, for batch replay) ---------------------------


class BatchBook:
    """Mutable book state for batch reconstruction."""

    def __init__(self):
        # side -> {price_cents: qty}
        self.sides: dict[str, dict[int, int]] = {"yes": {}, "no": {}}

    def load_snapshot(self, levels: pl.DataFrame) -> None:
        """Initialize from a snapshot DataFrame with columns: side, price_cents, qty."""
        self.sides = {"yes": {}, "no": {}}
        for row in levels.iter_rows(named=True):
            s, p, q = row["side"], row["price_cents"], row["qty"]
            if q > 0:
                self.sides[s][p] = q

    def apply_delta(self, side: str, price_cents: int, delta: int) -> None:
        book = self.sides[side]
        new_qty = max(0, book.get(price_cents, 0) + delta)
        if new_qty == 0:
            book.pop(price_cents, None)
        else:
            book[price_cents] = new_qty

    def emit(self, market_ticker: str, ts: int) -> list[dict]:
        """Emit one row per active price level."""
        rows = []
        for side in ("yes", "no"):
            for price_cents, qty in self.sides[side].items():
                rows.append({
                    "market_ticker": market_ticker,
                    "ts": ts,
                    "side": side,
                    "price_cents": price_cents,
                    "qty": qty,
                })
        return rows

    def to_dict(self) -> dict[str, dict[int, int]]:
        """Return a deep copy for verification."""
        return {
            s: dict(levels) for s, levels in self.sides.items()
        }


# -- Verification --------------------------------------------------------


def verify_boundary(
    book: BatchBook,
    snapshot_levels: pl.DataFrame,
    market_ticker: str,
    ts: int,
) -> bool:
    """Compare reconstructed book state against a snapshot.

    Returns True if they match exactly.
    """
    expected: dict[str, dict[int, int]] = {"yes": {}, "no": {}}
    for row in snapshot_levels.iter_rows(named=True):
        s, p, q = row["side"], row["price_cents"], row["qty"]
        if q > 0:
            expected[s][p] = q

    actual = book.to_dict()

    if actual == expected:
        return True

    # Log differences.
    for side in ("yes", "no"):
        a_levels = actual.get(side, {})
        e_levels = expected.get(side, {})
        all_prices = set(a_levels) | set(e_levels)
        for p in sorted(all_prices):
            aq = a_levels.get(p, 0)
            eq = e_levels.get(p, 0)
            if aq != eq:
                log.warning(
                    "DRIFT %s %s %dc: reconstructed=%d snapshot=%d",
                    market_ticker,
                    side,
                    p,
                    aq,
                    eq,
                )
    return False


# -- Reconstruction ------------------------------------------------------


def reconstruct_market(
    market_ticker: str,
    snapshots: pl.DataFrame,
    deltas: pl.DataFrame,
) -> list[dict]:
    """Reconstruct full LOB history for a single market.

    Args:
        market_ticker: The market ticker.
        snapshots: DataFrame with columns: ts, side, price_cents, qty.
        deltas: DataFrame with columns: ts, side, price_cents, delta.

    Returns:
        List of dicts (one per active level per tick) ready for Parquet.
    """
    # Get unique snapshot timestamps, sorted.
    snap_ts = snapshots.select("ts").unique().sort("ts")["ts"].to_list()

    if not snap_ts:
        log.warning("No snapshots for %s, skipping", market_ticker)
        return []

    all_rows: list[dict] = []
    book = BatchBook()
    verified = 0
    failed = 0

    for i, s_ts in enumerate(snap_ts):
        # Initialize book from snapshot S_i.
        snap_levels = snapshots.filter(pl.col("ts") == s_ts)
        book.load_snapshot(snap_levels)

        # Emit initial state.
        all_rows.extend(book.emit(market_ticker, s_ts))

        # Determine window end.
        if i + 1 < len(snap_ts):
            next_ts = snap_ts[i + 1]
        else:
            # Last window: replay all remaining deltas.
            next_ts = deltas["ts"].max() + 1 if len(deltas) > 0 else s_ts + 1

        # Get deltas in window [s_ts, next_ts).
        window_deltas = deltas.filter(
            (pl.col("ts") >= s_ts) & (pl.col("ts") < next_ts)
        ).sort("ts")

        if len(window_deltas) == 0:
            continue

        # Group by timestamp and apply.
        for ts_val, group in window_deltas.group_by("ts", maintain_order=True):
            ts = ts_val[0]
            for row in group.iter_rows(named=True):
                book.apply_delta(row["side"], row["price_cents"], row["delta"])
            all_rows.extend(book.emit(market_ticker, ts))

        # Verify at boundary if next snapshot exists.
        if i + 1 < len(snap_ts):
            next_snap = snapshots.filter(pl.col("ts") == next_ts)
            if verify_boundary(book, next_snap, market_ticker, next_ts):
                verified += 1
            else:
                failed += 1

    log.info(
        "%s: emitted %d rows, %d windows verified, %d drifted",
        market_ticker,
        len(all_rows),
        verified,
        failed,
    )
    return all_rows


def reconstruct_day(date_str: str, config: ReconstructConfig | None = None):
    """Reconstruct all markets for a single date partition.

    Reads bronze, reconstructs, writes silver LOB Parquet to S3.
    """
    cfg = config or ReconstructConfig()
    s3 = boto3.client("s3")

    # Load bronze data for this date.
    delta_prefix = f"{cfg.bronze_prefix}/deltas/dt={date_str}/"
    snap_prefix = f"{cfg.bronze_prefix}/snapshots/dt={date_str}/"

    log.info("Loading bronze data for %s...", date_str)
    all_deltas = _load_all_parquet(s3, cfg.bucket, delta_prefix)
    all_snapshots = _load_all_parquet(s3, cfg.bucket, snap_prefix)

    if len(all_snapshots) == 0:
        log.warning("No snapshots for %s, cannot reconstruct", date_str)
        return

    # Get unique tickers.
    tickers = all_snapshots.select("market_ticker").unique()["market_ticker"].to_list()
    log.info("Reconstructing %d markets for %s", len(tickers), date_str)

    all_lob_rows: list[dict] = []

    for ticker in tickers:
        t_deltas = all_deltas.filter(pl.col("market_ticker") == ticker)
        t_snaps = all_snapshots.filter(pl.col("market_ticker") == ticker)
        rows = reconstruct_market(ticker, t_snaps, t_deltas)
        all_lob_rows.extend(rows)

    if not all_lob_rows:
        log.warning("No LOB rows produced for %s", date_str)
        return

    # Write silver.
    lob_df = pl.DataFrame(all_lob_rows)
    arrow_table = lob_df.to_arrow()
    buf = io.BytesIO()
    pq.write_table(arrow_table, buf, compression="zstd")
    buf.seek(0)

    key = f"{cfg.silver_prefix}/lob/dt={date_str}/lob.parquet"
    s3.put_object(Bucket=cfg.bucket, Key=key, Body=buf.getvalue())
    log.info(
        "Wrote %d silver LOB rows → s3://%s/%s",
        len(all_lob_rows),
        cfg.bucket,
        key,
    )
