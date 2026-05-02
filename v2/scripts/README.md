# v2 Scripts

- **live ingester** = streams Kalshi WS data to bronze + silver (v=3), runs continuously
- **compact** = defragmentation (many small files to one big file), run daily
- **backfill** = format conversion (v=2 to v=3), run once

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

**Problem:** v=2 silver files are missing fields (`t_exchange`, `sid`, `seq`), use float64 timestamps, have no dictionary encoding, and no sort guarantee. v=3 needs all of these fixed.

**What it does:** Two-phase backfill depending on event type:

1. **OrderBookUpdate / TradeEvent / BookInvalidated** — re-derived from **bronze** (raw WS frames) by replaying through the transform. This is the only way to get `t_exchange` (Kalshi's server timestamp), `sid` (subscription ID), and `seq` (sequence number), since v=2 silver never captured them.
2. **MM event types** (MMQuoteEvent, MMFillEvent, etc.) — converted from **v=2 silver**. These events don't have the new fields, so null columns are added.

Output is already compacted (one file per date per event type).

- **Reads from (bronze):** `s3://prediction-markets-data/bronze/kalshi_ws/{channel}/{Y}/{M}/{D}/{H}/*.jsonl.gz`
- **Reads from (silver):** `s3://prediction-markets-data/silver/kalshi_ws/{EventType}/date={YYYY-MM-DD}/v=2/*.parquet`
- **Writes to:** `s3://prediction-markets-data/silver/kalshi_ws/{EventType}/date={YYYY-MM-DD}/v=3/compacted-{uuid}.parquet`

**When to run:** Once, after deploying v2. Use `--delete-existing` to re-derive if v=3 already exists (e.g., after schema changes).

```bash
python -m v2.scripts.backfill_silver_v3 --dry-run
python -m v2.scripts.backfill_silver_v3
python -m v2.scripts.backfill_silver_v3 --event-type OrderBookUpdate
python -m v2.scripts.backfill_silver_v3 --delete-existing    # re-derive, replacing existing v=3
```
