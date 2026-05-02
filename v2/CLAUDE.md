# v2 — S3 Order Book Data Infrastructure

Rebuild of the data layer for Kalshi order book data. v1 (live MM strategy) continues on EC2. v2 focuses on storage, query, replay, and data quality.

## Key rules

- Integer cents for money — no floats. Kalshi sends 4-decimal dollar strings, converted via `_dollars_to_cents()`.
- `t_receipt` and `t_exchange` stay as `float` (seconds) in Python dataclasses. Conversion to `int64` nanoseconds happens only at the silver writer serialization boundary.
- `SILVER_VERSION = 3`. Bump on breaking schema changes — old and new partitions coexist.
- Bronze is authoritative. Silver is rebuildable from bronze via backfill.

## Commands

```bash
source .venv/bin/activate

# Tests
python -m pytest v2/tests/ -v

# Live ingester (data-only, no strategy)
python -m v2.scripts.live.kalshi_ws

# Backfill v=3 from bronze (includes t_exchange, sid, seq)
python -m v2.scripts.backfill_silver_v3 --dry-run
python -m v2.scripts.backfill_silver_v3 --delete-existing

# Compact yesterday's small files into one sorted file
python -m v2.scripts.compact_silver --date YYYY-MM-DD

# Compaction runs daily at 07:00 UTC / 2:00 AM EST via systemd timer (kalshi-v2-compact.timer)
# Manual trigger on EC2: sudo systemctl start kalshi-v2-compact.service
```

## Environment

- Python 3.12, `.venv/` at repo root (shared with v1)
- Dependencies: `pyarrow`, `polars`, `duckdb`, `boto3`
- S3 bucket: `prediction-markets-data`
- AWS Glue database: `prediction_markets` (Phase 2)

## Docs

- `docs/ec2-deploy.md` — systemd deployment on EC2
- `docs/plan.md` — implementation plan (phases 1–5)
- `docs/parquet-storage-engineering.md` — encoding, compression, query optimization
- `docs/why-compaction.md` — why small files hurt and how compaction fixes it
- `app/README.md` — event field reference (what each silver column is and why)
- `scripts/README.md` — what each script does, reads from, writes to
