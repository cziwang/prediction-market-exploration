# v2 Scripts

- **live ingester** = streams Kalshi WS data to bronze + silver (v=3), runs continuously
- **compact** = defragmentation (many small files to one big file), run daily
- **backfill** = re-derive silver v=3 from bronze, run after schema changes or `_parse_ts` fixes
- **infra** = AWS Glue/Athena setup, S3 cleanup
- **replay** = full-depth order book reconstruction from bronze for research/backtesting

## live/kalshi_ws/

**What it does:** Connects to Kalshi's WebSocket, subscribes to `orderbook_delta` + `trade` for all NBA series, and writes data to two layers simultaneously:

- **Bronze:** raw frames as gzip-JSONL to `s3://prediction-markets-data/bronze/kalshi_ws/{channel}/{Y}/{M}/{D}/{H}/{uuid}.jsonl.gz`
- **Silver:** typed events as Parquet v=3 to `s3://prediction-markets-data/silver/kalshi_ws/{EventType}/date={YYYY-MM-DD}/v=3/part-{uuid}.parquet`

Data-only — no trading strategy. The transform converts raw WS frames into typed events (`OrderBookUpdate`, `TradeEvent`, `BookInvalidated`) with integer cents for prices and float seconds for timestamps internally. The silver writer converts timestamps to int64 nanoseconds and applies dictionary encoding at the serialization boundary.

**Requires:** `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in `.env` (Kalshi requires auth even for public channels).

**When to run:** Continuously on EC2 via systemd. Reconnects automatically with exponential backoff. Clean shutdown on SIGINT/SIGTERM (flushes both writers).

```bash
python -m v2.scripts.live.kalshi_ws
```

## compact_silver.py

**Problem:** The live writer flushes every 60s, producing dozens of tiny files per day. Athena/Spark perform poorly on many small files — they need fewer, larger files (128 MB+ target).

**What it does:** Merges all small `part-*.parquet` files from a single day into one sorted file, then deletes the originals.

- **Reads from:** `s3://prediction-markets-data/silver/kalshi_ws/{EventType}/date={YYYY-MM-DD}/v={version}/part-*.parquet`
- **Writes to:** `s3://prediction-markets-data/silver/kalshi_ws/{EventType}/date={YYYY-MM-DD}/v={version}/compacted-{uuid}.parquet`
- Concatenates all part files, sorts by `t_receipt_ns`
- Verifies row count matches before deleting the original part files
- Refuses to compact today's date (live writer may be active)

**When to run:** Daily via cron, for yesterday's date.

```bash
python -m v2.scripts.compact_silver --date 2026-04-30 --dry-run
python -m v2.scripts.compact_silver --date 2026-04-30
python -m v2.scripts.compact_silver --event-type OrderBookUpdate --date 2026-04-30
```

## backfill_silver_v3.py

**What it does:** Re-derives silver v=3 from bronze data.

1. **OrderBookUpdate / TradeEvent / BookInvalidated** — re-derived from **bronze** (raw WS frames) by replaying through `KalshiTransform`. Produces BBO-level data with `t_exchange_ns`, `sid`, and `seq`.
2. **MM event types** (MMQuoteEvent, MMFillEvent, etc.) — if v=2 silver still exists for these, converts them to v=3 schema. Otherwise skips (v=2 has been deleted).

Note: this produces BBO-only silver data. For full-depth order books, use `replay/build_books.py` instead.

- **Reads from:** `s3://prediction-markets-data/bronze/kalshi_ws/{channel}/{Y}/{M}/{D}/{H}/*.jsonl.gz`
- **Writes to:** `s3://prediction-markets-data/silver/kalshi_ws/{EventType}/date={YYYY-MM-DD}/v=3/compacted-{uuid}.parquet`

**When to run:** After schema changes or bug fixes (e.g., `_parse_ts` ISO string fix). Use `--delete-existing` to re-derive dates that already have v=3.

```bash
python -m v2.scripts.backfill_silver_v3 --dry-run
python -m v2.scripts.backfill_silver_v3
python -m v2.scripts.backfill_silver_v3 --event-type OrderBookUpdate
python -m v2.scripts.backfill_silver_v3 --delete-existing    # re-derive, replacing existing v=3
```

## infra/setup_glue_catalog.py

**What it does:** Creates the AWS Glue Data Catalog database, one table per silver event type, and an Athena workgroup — all idempotent. Re-running updates existing table definitions (useful after schema changes).

- **Database:** `prediction_markets`
- **Tables:** 8 tables (`order_book_update`, `trade_event`, `book_invalidated`, `mm_quote_event`, `mm_order_event`, `mm_fill_event`, `mm_reconcile_event`, `mm_circuit_breaker_event`)
- **Partition projection:** `date` (string, yyyy-MM-dd) + `v` (integer, 3 only) — Athena computes partitions from rules, no `MSCK REPAIR TABLE` needed
- **Workgroup:** `prediction-markets` with 10 GB scan cutoff, results to `s3://prediction-markets-data/athena-results/`

**When to run:** Once to set up, or again after silver schema changes.

```bash
python -m v2.scripts.infra.setup_glue_catalog --dry-run
python -m v2.scripts.infra.setup_glue_catalog
```

## replay/build_books.py

**What it does:** Reconstructs full-depth order books from bronze `orderbook_snapshot` + `orderbook_delta` data. Replays events in sequence order and writes point-in-time book snapshots to Parquet.

- **Reads from:** `s3://prediction-markets-data/bronze/kalshi_ws/orderbook_snapshot/` and `orderbook_delta/`
- **Writes to:** local Parquet file (one row per price level per snapshot)
- Output includes: both sides' full depth (all price levels + sizes), BBO, exchange timestamps
- Configurable snapshot frequency: `every_event` (after every delta) or `every_n` (every N deltas per ticker)

**When to run:** On-demand for research/backtesting. Not a scheduled job.

```bash
# All tickers, snapshot every 100 deltas
python -m v2.scripts.replay.build_books --date 2026-04-26

# Single ticker, every event
python -m v2.scripts.replay.build_books \
    --date 2026-04-26 \
    --ticker KXNBAGAME-26APR25DENMIN-DEN \
    --snapshot-mode every_event \
    --output books.parquet

# Dry run — stats only
python -m v2.scripts.replay.build_books --date 2026-04-26 --dry-run
```
