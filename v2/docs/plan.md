# v2 — S3 Order Book Data Infrastructure

**Goal:** Rebuild the data layer from the ground up with proper storage layout, a query catalog, a replay engine, and data quality checks. The live MM strategy (v1) continues running on EC2 unchanged — v2 focuses on the data infrastructure that feeds research and backtesting.

**Principle:** Get the storage layout right first. Every downstream system inherits its performance from the data shape on S3.

---

## What we have (v1, still running)

- **Bronze:** gzip-JSONL at `bronze/kalshi_ws/{channel}/{Y}/{M}/{D}/{H}/{uuid}.jsonl.gz`
- **Silver:** Parquet (zstd) at `silver/kalshi_ws/{EventType}/date={YYYY-MM-DD}/v=2/part-{uuid}.parquet`
- **Materialized:** single Parquet per channel at `materialized/kalshi_ws/{channel}.parquet`
- Timestamps: `float64` (Unix seconds) — imprecise, wastes bytes
- No explicit schemas — inferred at write time via `pa.Table.from_pylist()`
- No dictionary encoding for strings, no sort guarantee within files
- File sizes: hundreds of KB (target: 128 MB–1 GB)
- No query catalog, no Athena, no data quality checks

---

## Phase 1: Storage Layout

The highest-leverage change. Fix the Parquet files so every downstream system is fast by default.

### 1a. Explicit Arrow schemas

Define a `pa.Schema` per event type with:

| Column type | Encoding |
|---|---|
| `t_receipt_ns` (int64, nanoseconds since epoch) | Delta encoding — timestamps are monotonic, deltas compress to near-zero |
| `market_ticker`, `side`, `action`, `reason`, `field`, `state`, `error` | Dictionary encoding (`pa.dictionary(pa.int16(), pa.utf8())`) |
| Price/size integers (`bid_yes`, `ask_yes`, `price`, `size`, etc.) | `pa.int32()` — no floats for money |
| Nullable fields (`bid_price | None`, `order_id | None`, etc.) | Appropriate type with `nullable=True` |

Schemas for all 8 event types: `OrderBookUpdate`, `TradeEvent`, `BookInvalidated`, `MMQuoteEvent`, `MMOrderEvent`, `MMFillEvent`, `MMReconcileEvent`, `MMCircuitBreakerEvent`.

### 1b. Writer changes

New `SilverWriter` (or refactored from v1):
- Convert `t_receipt` (float seconds) → `t_receipt_ns` (int64 nanoseconds) at the serialization boundary. Event dataclasses keep `float` internally — the strategy does `t_receipt - opened_at` arithmetic in seconds.
- Sort rows by `t_receipt_ns` before writing. Parquet row-group statistics then let query engines skip huge chunks on time-range filters — often 10x speedup.
- Use explicit schema in `pa.Table.from_pylist(rows, schema=schema)` instead of inference.
- Set `row_group_size=100_000` for predicate pushdown within files.
- Compression: zstd (already using, keep it — beats Snappy on ratio with negligible CPU).
- Add `min_rows=1000` flush guard to reduce tiny-file proliferation.

### 1c. Version bump

`SILVER_VERSION = 3`. New files land in `v=3/` partitions. v=2 files remain readable.

### 1d. Post-hoc compaction

Script: `scripts/compact_silver.py`
- Reads all `part-*.parquet` for a date/event-type/version partition
- Merges → sorts by `t_receipt_ns` → writes single compacted file
- Verifies row count, deletes originals
- Safety: only processes dates strictly before today
- Run daily via cron

Target file sizes: 128 MB–1 GB. Given NBA props volume (~low single-digit GB/day), one compacted file per event type per date is likely right.

### 1e. Backfill v=2 → v=3

One-time script: `scripts/backfill_silver_v3.py`
- For each historical date + event type: read v=2, convert timestamps, apply explicit schema, sort, write single v=3 file
- Leave v=2 in place for backward compat

### 1f. Iceberg decision

**Not now.** Data volume is modest, one writer, handful of notebook consumers. Plain Hive-partitioned Parquet with Glue partition projection gives us Athena + SQL immediately. Architecture is designed so PyIceberg can be layered on later via the same Glue catalog tables when we need schema evolution, ACID writes, or time travel.

---

## Phase 2: Glue Data Catalog + Athena

SQL over the entire S3 dataset with zero infra. Pay-per-query, no cluster.

### 2a. Glue catalog setup

Script: `scripts/infra/setup_glue_catalog.py` (boto3, idempotent)
- Database: `prediction_markets`
- One table per event type pointing at `s3://prediction-markets-data/silver/kalshi_ws/{EventType}/`
- Partition keys: `date` (string), `v` (int)
- **Partition projection** enabled — avoids `MSCK REPAIR TABLE` overhead:
  ```
  projection.enabled = true
  projection.date.type = date
  projection.date.format = yyyy-MM-dd
  projection.date.range = 2026-04-01,NOW
  projection.v.type = integer
  projection.v.range = 2,3
  ```

### 2b. Athena workgroup

Workgroup `prediction-markets`:
- Output: `s3://prediction-markets-data/athena-results/`
- Engine: Athena v3 (Trino)
- Cost guard: `BytesScannedCutoffPerQuery = 10 GB`

### 2c. Example queries

Document reference queries: spread distribution, trade lookups, fill reconciliation, ASOF-style joins via window functions.

### 2d. Deprecate materialized layer

The `materialized/kalshi_ws/{channel}.parquet` single-file approach doesn't scale and will be replaced by Athena queries over partitioned silver. Add deprecation note; don't delete yet.

---

## Phase 3: Replay / Snapshot Tool

The single most valuable piece of code in any tick-data stack: events → book state at arbitrary timestamps.

### 3a. ReplayBookState

Extends v1's `OrderBookState` (`dict[int, int]` price→size maps):
- Adds `seq: int | None` for sequence tracking
- Adds `last_snapshot_ns: int` for timestamp tracking
- `to_snapshot() -> dict` — full book state serialized
- `validate() -> list[str]` — invariant checks (no crossed book, no negative sizes, BBO in 0–100)

### 3b. ReplayEngine

```python
class ReplayEngine:
    def __init__(self, *, snapshot_mode, validate=True, alert_on_gaps=True): ...
    def process_event(self, event: dict) -> BookSnapshot | None: ...
    def get_book(self, ticker: str) -> ReplayBookState | None: ...
```

- Maintains `dict[str, ReplayBookState]` per ticker
- Processes events in timestamp order (pre-sorted after Phase 1)
- Tracks seq numbers per `(sid, channel)`, alerts on gaps
- Snapshot modes: `every_event`, `every_n`, `every_us`, `on_demand`
- Validates invariants after every delta

**Data source:** Silver `OrderBookUpdate` only has BBO. Full-depth replay requires bronze `orderbook_delta` + `orderbook_snapshot` (per-level deltas). The engine handles both.

### 3c. CLI entry point

```bash
python -m scripts.replay.build_books \
    --ticker KXNBAPTS-26APR30-JOKIC-O32 \
    --date 2026-04-30 \
    --snapshot-mode every_n --snapshot-interval 100 \
    --output snapshots.parquet
```

Streams from Parquet via Polars `scan_parquet().filter().collect(streaming=True)` — doesn't load all in memory.

### 3d. Future: Rust port

Hot loop is `process_event()` → `apply_delta()` → sorted price-level map lookups. When performance matters, port to Rust via PyO3 as a `BookBuilder` Python extension. Many shops end up here.

---

## Phase 4: Hot Tier (deferred)

When Athena's 30s latency becomes unacceptable for iterative microstructure research:

- **ClickHouse on EC2** (i4i/i7ie with NVMe): sub-second queries on billions of rows, native ASOF joins, MergeTree for append-heavy time series. Default recommendation.
- **DuckDB on a single big EC2**: reads Parquet from S3 directly, near-zero data movement. Great for 1–2 researchers, doesn't scale to concurrent users.

Avoid Redshift (wrong access patterns) and EMR/Spark (overkill for this volume).

---

## Phase 5: Data Quality & Ops

Silent data loss is the thing that bites everyone. Build detection from day one.

### 5a. Sequence gap detection

`GapDetector` class tracking `last_seq` per `(sid, channel)`. CLI script reads bronze for a date, reports gaps. Non-zero exit code for CI/cron.

### 5b. Book invariant checks

Built into `ReplayBookState.validate()`:
- No crossed book: `best_bid < best_ask`
- No negative sizes at any price level
- BBO sanity: 0 < price <= 100
- All sizes positive integers

### 5c. S3 lifecycle rules

Script: `scripts/infra/setup_s3_lifecycle.py`

| Prefix | Standard | S3-IA | Glacier Deep Archive | Expire |
|---|---|---|---|---|
| `bronze/` | 90 days | +90 days | after that | never |
| `silver/` | 90 days | after that | — | never |
| `athena-results/` | — | — | — | 7 days |
| `materialized/` | 30 days | after that | — | 180 days |

### 5d. Daily quality report

Row counts, file counts/sizes, gap count, invariant violations. Extensible to SNS alerting.

---

## v2 directory structure (planned)

```
v2/
├── docs/
│   ├── plan.md                    # this file
│   └── athena-queries.md          # reference SQL
├── app/
│   ├── core/
│   │   └── config.py              # S3_BUCKET, SILVER_VERSION=3
│   ├── events.py                  # same event dataclasses (copied from v1)
│   ├── services/
│   │   └── silver_writer.py       # explicit schemas, int64 ns, sorting, dict encoding
│   ├── replay/
│   │   ├── book_state.py          # ReplayBookState with seq tracking + validation
│   │   └── engine.py              # ReplayEngine — events → book snapshots
│   └── quality/
│       └── gap_detector.py        # sequence gap detection
├── scripts/
│   ├── compact_silver.py          # post-hoc compaction
│   ├── backfill_silver_v3.py      # one-time v2→v3 migration
│   ├── infra/
│   │   ├── setup_glue_catalog.py  # Glue + Athena setup
│   │   └── setup_s3_lifecycle.py  # S3 tiering rules
│   ├── replay/
│   │   └── build_books.py         # replay CLI
│   └── quality/
│       ├── check_gaps.py          # gap detection CLI
│       └── daily_report.py        # daily quality summary
├── tests/
├── requirements.txt               # pyarrow, polars, duckdb, boto3
└── CLAUDE.md
```

---

## Implementation order

| Step | Phase | What | Depends on |
|---|---|---|---|
| 1 | 1a–1c | Explicit schemas + writer changes + version bump | nothing |
| 2 | 1d | Compaction script | step 1 |
| 3 | 1e | Backfill v=2 → v=3 | step 1 |
| 4 | 5c | S3 lifecycle rules | nothing (independent) |
| 5 | 2a–2b | Glue catalog + Athena workgroup | step 1 (v=3 files on S3) |
| 6 | 3a–3c | Replay engine + CLI | step 1 (sorted files) |
| 7 | 5a–5b, 5d | Gap detection + invariant checks + daily report | step 6 (replay engine) |
