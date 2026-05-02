"""Build full-depth order book snapshots from bronze data.

Reads bronze orderbook_snapshot + orderbook_delta for a date, replays
them in sequence order, and writes full-depth book snapshots to Parquet.

Output: one row per price level per snapshot. A single book snapshot for
one ticker with 50 yes levels and 40 no levels produces 90 rows.

Usage:
    # All tickers, snapshot every 100 deltas
    python -m v2.scripts.replay.build_books --date 2026-04-26

    # Single ticker, snapshot every event
    python -m v2.scripts.replay.build_books \
        --date 2026-04-26 \
        --ticker KXNBAGAME-26APR25DENMIN-DEN \
        --snapshot-mode every_event \
        --output books.parquet

    # Dry run — stats only, no output file
    python -m v2.scripts.replay.build_books --date 2026-04-26 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import time

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from v2.app.replay.engine import ReplayEngine

log = logging.getLogger(__name__)

SCHEMA = pa.schema([
    ("t_receipt_ns", pa.int64()),
    ("t_exchange_ns", pa.int64()),
    ("market_ticker", pa.dictionary(pa.int16(), pa.utf8())),
    ("seq", pa.int32()),
    ("sid", pa.int32()),
    ("side", pa.dictionary(pa.int16(), pa.utf8())),
    ("price_cents", pa.int32()),
    ("size", pa.int32()),
    ("best_bid", pa.int32()),
    ("best_ask", pa.int32()),
    ("bid_size", pa.int32()),
    ("ask_size", pa.int32()),
])


def snapshots_to_table(snapshots) -> pa.Table:
    """Convert list of BookSnapshot to Arrow table (one row per level)."""
    rows = []
    for snap in snapshots:
        # Shared fields for every row in this snapshot
        shared = {
            "t_receipt_ns": snap.t_receipt_ns,
            "t_exchange_ns": snap.t_exchange_ns,
            "market_ticker": snap.market_ticker,
            "seq": snap.seq,
            "sid": snap.sid,
            "best_bid": snap.best_bid,
            "best_ask": snap.best_ask,
            "bid_size": snap.bid_size,
            "ask_size": snap.ask_size,
        }
        for price, size in snap.yes_levels:
            rows.append({**shared, "side": "yes", "price_cents": price, "size": size})
        for price, size in snap.no_levels:
            rows.append({**shared, "side": "no", "price_cents": price, "size": size})

    if not rows:
        return pa.Table.from_pylist([], schema=SCHEMA)

    return pa.Table.from_pylist(rows, schema=SCHEMA)


def main():
    parser = argparse.ArgumentParser(
        description="Build full-depth book snapshots from bronze data",
    )
    parser.add_argument("--date", required=True, help="Date to replay (YYYY-MM-DD)")
    parser.add_argument("--ticker", action="append", dest="tickers",
                        help="Filter to specific ticker(s). Repeat for multiple. Omit for all.")
    parser.add_argument("--snapshot-mode", default="every_n",
                        choices=["every_event", "every_n"],
                        help="When to emit snapshots (default: every_n)")
    parser.add_argument("--snapshot-interval", type=int, default=100,
                        help="N for every_n mode (default: 100)")
    parser.add_argument("--output", "-o", help="Output Parquet path (default: books-{date}.parquet)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print stats only, don't write output")
    parser.add_argument("--no-validate", action="store_true",
                        help="Skip book validation (faster)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ticker_set = set(args.tickers) if args.tickers else None
    output_path = args.output or f"books-{args.date}.parquet"

    s3 = boto3.client("s3")
    engine = ReplayEngine(s3)

    log.info("replaying %s (tickers=%s, mode=%s, interval=%d)",
             args.date,
             ticker_set or "all",
             args.snapshot_mode,
             args.snapshot_interval)

    t0 = time.time()
    snapshots, stats = engine.replay(
        args.date,
        tickers=ticker_set,
        snapshot_mode=args.snapshot_mode,
        snapshot_interval=args.snapshot_interval,
        validate=not args.no_validate,
    )
    elapsed = time.time() - t0

    log.info("replay complete in %.1fs", elapsed)
    log.info("  records processed: %s", f"{stats.records_processed:,}")
    log.info("  snapshots processed: %s", f"{stats.snapshots_processed:,}")
    log.info("  deltas processed: %s", f"{stats.deltas_processed:,}")
    log.info("  seq gaps: %s", f"{stats.seq_gaps:,}")
    log.info("  validation errors: %s", f"{stats.validation_errors:,}")
    log.info("  book snapshots emitted: %s", f"{stats.snapshots_emitted:,}")
    log.info("  tickers seen: %s", f"{stats.tickers_seen:,}")

    if args.dry_run:
        log.info("[DRY RUN] would write %d snapshots to %s", stats.snapshots_emitted, output_path)
        return

    if not snapshots:
        log.info("no snapshots to write")
        return

    table = snapshots_to_table(snapshots)
    pq.write_table(table, output_path, compression="zstd")

    log.info("wrote %s (%d rows, %.1f MB)",
             output_path, len(table),
             table.nbytes / (1024 * 1024))


if __name__ == "__main__":
    main()
