# v2/scripts/replay

Order book reconstruction tools for research and backtesting.

## build_books.py

Reconstructs full-depth order books from bronze `orderbook_snapshot` + `orderbook_delta` data. Replays events in sequence order and writes point-in-time book snapshots to local Parquet.

- **Reads from:** `s3://prediction-markets-data/bronze/kalshi_ws/orderbook_snapshot/` and `orderbook_delta/`
- **Writes to:** local Parquet file (one row per price level per snapshot)
- Output includes both sides' full depth (all price levels + sizes), BBO, and exchange timestamps
- Configurable snapshot frequency: `every_event` or `every_n` (default: every 100 deltas per ticker)

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
