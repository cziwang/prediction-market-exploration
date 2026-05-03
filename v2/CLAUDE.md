# v2 — S3 Order Book Data Infrastructure

Rebuild of the data layer for Kalshi order book data. v1 (live MM strategy) continues on EC2. v2 focuses on storage, query, replay, and data quality.

## Key rules

- Integer cents for money — no floats. Kalshi sends 4-decimal dollar strings, converted via `dollars_to_cents()` in `app/core/conversions.py`.
- `t_receipt` and `t_exchange` stay as `float` (seconds) internally. Conversion to `int64` nanoseconds happens at the silver writer serialization boundary.
- `SILVER_VERSION = 3`. Bump on breaking schema changes — old and new partitions coexist.
- Bronze is authoritative. Silver is rebuildable from bronze via `build_silver`.
- `BookState` in `app/core/book_state.py` is the single canonical order book class. Used by both the live ingester and batch replay.
- `OrderBookDepth` (53-column, top-10 depth) is the primary book data table. `OrderBookUpdate` (BBO-only) is deprecated.

## Commands

```bash
source .venv/bin/activate

# Tests (40)
python -m pytest v2/tests/ -v

# Live ingester (produces bronze + OrderBookDepth + TradeEvent + BookInvalidated)
python -m v2.scripts.live.kalshi_ws

# Build silver from bronze (unified backfill + depth builder)
python -m v2.scripts.build_silver --date 2026-04-26
python -m v2.scripts.build_silver --start 2026-04-22 --end 2026-05-01
python -m v2.scripts.build_silver --date 2026-04-26 --dry-run

# Compact yesterday's small files into one sorted file
python -m v2.scripts.compact_silver --date YYYY-MM-DD

# Fetch/refresh market metadata from Kalshi REST API
python -m v2.scripts.fetch_markets

# Compaction runs daily at 07:00 UTC via systemd timer (kalshi-v2-compact.timer)
```

## Environment

- Python 3.12, `.venv/` at repo root (shared with v1)
- Dependencies: `pyarrow`, `polars`, `duckdb`, `boto3`
- S3 bucket: `prediction-markets-data`
- AWS Glue database: `prediction_markets`

## Docs

- `docs/backtest-readiness.md` — data assets, pipeline architecture, refactor plan
- `docs/ec2-deploy.md` — systemd deployment on EC2
- `docs/parquet-storage-engineering.md` — encoding, compression, query optimization
- `docs/why-compaction.md` — why small files hurt and how compaction fixes it
- `app/README.md` — event field reference (what each silver column is and why)
- `scripts/README.md` — what each script does, reads from, writes to
