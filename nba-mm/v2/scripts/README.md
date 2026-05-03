# v2 Scripts

## live/kalshi_ws/

Streams Kalshi WS data to bronze + silver. Runs continuously on EC2.

Produces:
- **Bronze:** raw frames as gzip-JSONL
- **Silver:** `OrderBookDepth` (53-column depth), `TradeEvent`, `BookInvalidated`

```bash
python -m v2.scripts.live.kalshi_ws
```

## build_silver.py

Unified batch builder: derives all silver types from bronze for one or more dates.

```bash
python -m v2.scripts.build_silver --all --delete-existing   # all dates from bronze
python -m v2.scripts.build_silver --date 2026-04-26
python -m v2.scripts.build_silver --start 2026-04-22 --end 2026-05-01
python -m v2.scripts.build_silver --all --dry-run            # preview
```

## compact_silver.py

Merges small `part-*.parquet` files into one sorted file per partition. Run daily for yesterday.

```bash
python -m v2.scripts.compact_silver --date 2026-04-30
python -m v2.scripts.compact_silver --date 2026-04-30 --dry-run
```

## fetch_markets.py

Fetches NBA market metadata from Kalshi REST API (live + historical). Writes to `s3://prediction-markets-data/reference/kalshi_markets.parquet`.

```bash
python -m v2.scripts.fetch_markets
python -m v2.scripts.fetch_markets --series KXNBAGAME
python -m v2.scripts.fetch_markets --dry-run
```

## infra/setup_glue_catalog.py

Creates/updates Glue catalog tables and Athena workgroup. Idempotent.

- 9 silver tables (incl. `order_book_depth`) + 1 reference table (`market_metadata`)
- Partition projection on `date` + `v=3`
- Workgroup: `prediction-markets` with 10 GB scan cutoff

```bash
python -m v2.scripts.infra.setup_glue_catalog
```

