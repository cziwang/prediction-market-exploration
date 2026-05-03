# S3 Order Book Data Revamp — Migration Tracker

**Created:** 2026-05-01
**Status:** Planning complete, implementation pending

## Overview

Revamp how we process and store S3 order book data across 4 phases: storage layout optimization, Glue/Athena catalog, replay/snapshot tooling, and data quality ops. Bronze layer (gzip-JSONL) is unchanged — it remains the authoritative raw archive.

**Key decision:** Plain Hive-partitioned Parquet (not Iceberg) for now. Data volume is modest (low single-digit GB/day). Architecture supports layering Iceberg later via PyIceberg + same Glue catalog.

---

## Phase 1: Storage Layout

**Goal:** Get the Parquet files in the right shape — proper types, encoding, sorting, file sizes. Highest leverage because every downstream system inherits performance from this.

### What changes

| Before (v=2) | After (v=3) |
|---|---|
| `t_receipt: float64` (seconds) | `t_receipt_ns: int64` (nanoseconds) |
| Schema inferred at write time | Explicit `pa.Schema` per event type |
| No dictionary encoding | Dictionary encoding for `market_ticker`, `side`, `action`, `reason`, `field`, `state`, `error` |
| No sort guarantee | Sorted by `t_receipt_ns` within each file |
| ~hundreds of KB per file | 128MB+ target via compaction |
| 60s flush, no row minimum | 60s flush + 1000-row minimum guard |
| `row_group_size` = default | `row_group_size` = 100,000 (enables predicate pushdown) |

### Tasks

- [ ] **1.1** Define explicit Arrow schemas in `app/services/silver_writer.py`
  - Schema registry: `dict[str, pa.Schema]` mapping event type name → schema
  - `t_receipt_ns: pa.int64()` replaces `t_receipt: float64`
  - `pa.dictionary(pa.int16(), pa.utf8())` for string columns
  - `pa.int32()` for price/size integers
  - Nullable types for `| None` fields
- [ ] **1.2** Modify `_flush_type()` in `app/services/silver_writer.py`
  - Convert `t_receipt` → `t_receipt_ns` at serialization boundary
  - Sort rows by `t_receipt_ns`
  - Use explicit schema in `pa.Table.from_pylist()`
  - Set `row_group_size=100_000`
  - Event dataclasses are NOT changed — conversion is serialization-only
- [ ] **1.3** Bump `SILVER_VERSION` in `app/core/config.py` from 2 → 3
- [ ] **1.4** Add `min_rows=1000` flush guard to `SilverWriter`
  - Skip flush if `len(events) < min_rows` unless shutdown drain
- [ ] **1.5** Build `scripts/compact_silver.py`
  - Reads all `part-*.parquet` for a date/event-type/version partition
  - Merges → sorts by `t_receipt_ns` → writes single compacted file
  - Verifies row count, deletes originals
  - Safety: only processes dates < today
- [ ] **1.6** Build `scripts/backfill_silver_v3.py`
  - One-time migration: v=2 → v=3 for all historical dates
  - Converts `t_receipt` to `t_receipt_ns`, applies explicit schema, sorts
  - v=2 files left in place
- [ ] **1.7** Update `scripts/mm_stats.py` reader for v=3 compat
  - If `t_receipt_ns` exists and `t_receipt` doesn't: derive `t_receipt = t_receipt_ns / 1e9`
- [ ] **1.8** Run tests: `python -m pytest tests/ -v` — all pass
- [ ] **1.9** Deploy updated live process, verify v=3 files on S3
- [ ] **1.10** Run backfill + compaction on historical data

### Files modified
- `app/services/silver_writer.py`
- `app/core/config.py`
- `scripts/mm_stats.py`

### Files created
- `scripts/compact_silver.py`
- `scripts/backfill_silver_v3.py`

### Risks & mitigations
- **Breaking live pipeline**: Version bump ensures new files go to `v=3/`, old `v=2/` untouched
- **Strategy arithmetic**: Event dataclasses unchanged — `t_receipt` stays `float` in Python objects
- **Compaction race**: Only compact dates < today, live writer only writes today

---

## Phase 2: Catalog + Ad-hoc Query

**Goal:** SQL over all S3 data with zero infra. Glue Data Catalog + Athena.

### Tasks

- [ ] **2.1** Build `scripts/infra/setup_glue_catalog.py`
  - Creates Glue database `prediction_markets`
  - Creates table per event type with:
    - Column defs matching v=3 schema
    - Partition keys: `date` (string), `v` (int)
    - Parquet SerDe
    - Partition projection enabled (no `MSCK REPAIR` needed):
      ```
      projection.enabled = true
      projection.date.type = date, format = yyyy-MM-dd, range = 2026-04-01,NOW
      projection.v.type = integer, range = 2,3
      ```
  - Creates Athena workgroup `prediction-markets`:
    - Output: `s3://prediction-markets-data/athena-results/`
    - Engine: Athena v3
    - Cost guard: `BytesScannedCutoffPerQuery = 10GB`
  - Script is idempotent (safe to re-run)
- [ ] **2.2** Test Athena queries in console
- [ ] **2.3** Write `docs/athena-queries.md` with reference queries
- [ ] **2.4** Add deprecation note to `scripts/materialize/bronze_to_parquet.py`

### Files created
- `scripts/infra/setup_glue_catalog.py`
- `docs/athena-queries.md`

---

## Phase 3: Replay/Snapshot Tool

**Goal:** Build the single most valuable piece of code in a tick-data stack — a function that takes order book events and produces book state at arbitrary timestamps.

### Architecture

```
Bronze (orderbook_delta + orderbook_snapshot)
    ↓ stream via Polars scan_parquet()
ReplayEngine
    ↓ maintains dict[ticker, ReplayBookState]
    ↓ validates invariants, detects seq gaps
BookSnapshot (full book state at a point in time)
    ↓ write to Parquet
Output file
```

Silver `OrderBookUpdate` only has BBO. Full depth replay requires bronze per-level deltas.

### Tasks

- [ ] **3.1** Build `app/replay/book_state.py`
  - `ReplayBookState` extending existing `OrderBookState` from `app/transforms/kalshi_ws.py`
  - Adds `seq: int | None`, `last_snapshot_ns: int`
  - `to_snapshot() -> dict` — serializable full book state
  - `validate() -> list[str]` — no crossed book, no negative sizes, BBO sanity
- [ ] **3.2** Build `app/replay/engine.py`
  - `ReplayEngine` class with configurable snapshot modes:
    - `every_event`, `every_n`, `every_us`, `on_demand`
  - Maintains `dict[str, ReplayBookState]` per ticker
  - Tracks seq numbers per `(sid, channel)`, alerts on gaps
  - Calls `validate()` after every delta
- [ ] **3.3** Build `scripts/replay/build_books.py`
  - CLI entry point for replay
  - Streams from Parquet via Polars (not load all in memory)
  - Outputs snapshots to local or S3 Parquet
- [ ] **3.4** Add `polars>=1.0` and `duckdb>=1.0` to `requirements.txt`
- [ ] **3.5** Test: replay known date, compare BBO output against existing silver `OrderBookUpdate`

### Files created
- `app/replay/__init__.py`
- `app/replay/book_state.py`
- `app/replay/engine.py`
- `scripts/replay/__init__.py`
- `scripts/replay/build_books.py`

### Files modified
- `requirements.txt`

### Future: Rust port
Hot loop is `ReplayEngine.process_event()` → `OrderBookState.apply_delta()`. When performance becomes a bottleneck, port to Rust via PyO3 as a `BookBuilder` extension.

---

## Phase 4: Hot Tier (deferred)

**Not implementing now.** When Athena latency hurts (>30s per query during iterative research):

- **Option A:** ClickHouse on EC2 (i4i instances, MergeTree engine, asof joins)
- **Option B:** DuckDB on a single big EC2 box (reads Parquet from S3 directly)

Avoid Redshift (wrong access patterns) and EMR/Spark (overkill).

---

## Phase 5: Data Quality & Ops

**Goal:** Detect silent data loss from day one.

### Tasks

- [ ] **5.1** Build `app/quality/gap_detector.py`
  - `GapDetector` class tracking `last_seq` per `(sid, channel)`
  - Returns `GapAlert` on discontinuities
- [ ] **5.2** Build `scripts/quality/check_gaps.py`
  - CLI: reads bronze for a date, parses `frame.sid`/`frame.seq`, reports gaps
  - Non-zero exit on gaps (CI/cron friendly)
- [ ] **5.3** Book invariant checks (built into `ReplayBookState.validate()` from Phase 3)
  - No crossed book: `best_bid < best_ask`
  - No negative sizes
  - BBO in 0-100 range
  - All sizes positive integers
- [ ] **5.4** Build `scripts/infra/setup_s3_lifecycle.py`
  - `bronze/` → Standard 90d → S3-IA 90d → Glacier Deep Archive
  - `silver/` → Standard 90d → S3-IA
  - `athena-results/` → Expire after 7d
  - `materialized/` → S3-IA after 30d → Expire after 180d
- [ ] **5.5** Build `scripts/quality/daily_report.py`
  - Row counts per event type, file counts/sizes, gap count, invariant violations
  - Extensible to SNS alerting later

### Files created
- `app/quality/__init__.py`
- `app/quality/gap_detector.py`
- `scripts/quality/check_gaps.py`
- `scripts/quality/daily_report.py`
- `scripts/infra/setup_s3_lifecycle.py`

---

## Implementation Order

| Priority | Phase | Dependency |
|---|---|---|
| 1 | Phase 1 (steps 1.1–1.4) | None — core writer changes |
| 2 | Phase 1 (steps 1.5–1.7) | 1.1–1.4 deployed |
| 3 | Phase 5.4 | None — S3 lifecycle rules are independent |
| 4 | Phase 2 | Phase 1 complete (v=3 files on S3) |
| 5 | Phase 3 | Phase 1 complete (sorted files) |
| 6 | Phase 5.1–5.3, 5.5 | Phase 3 complete (replay engine) |

---

## Verification checklist

- [ ] `python -m pytest tests/ -v` — all 66 tests pass after Phase 1 changes
- [ ] v=3 files on S3 have correct schema (check with `pyarrow.parquet.read_schema`)
- [ ] Compaction produces correct row counts
- [ ] Athena queries return results with partition projection (no `MSCK REPAIR`)
- [ ] Replay output matches existing silver `OrderBookUpdate` BBO data
- [ ] `check_gaps.py` on clean data reports zero gaps
- [ ] `aws s3api get-bucket-lifecycle-configuration` shows correct rules
