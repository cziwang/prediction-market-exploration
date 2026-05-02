# v2 — S3 Order Book Data Infrastructure

## Project overview

Rebuild the data layer for Kalshi prediction market order book data. v1 (live MM strategy, batch fetchers) continues running on EC2. v2 focuses on the storage, query, replay, and quality infrastructure that feeds research and backtesting.

## Key docs

- `v2/docs/plan.md` — full implementation plan (phases 1-5)
- `v2/docs/parquet-storage-engineering.md` — deep-dive on Parquet encoding, compression, and query optimization

## Environment

- Python 3.12, virtualenv at `.venv/` (shared with v1 at repo root)
- Dependencies: `pyarrow`, `polars`, `duckdb`, `boto3`
- AWS: S3 bucket `prediction-markets-data`, Glue database `prediction_markets`
- `SILVER_VERSION = 3` for v2 (v1 uses v=2)

## Common commands

```bash
# (placeholder — to be filled as v2 scripts are built)
```
