# Why Compaction Matters

## The problem

The live writer flushes to S3 every 60 seconds. On a busy game night, that's ~1,440 files per event type per day. Each file is a few hundred KB.

This creates a **small file problem** that hurts every downstream query:

**1. S3 API overhead.** Every Parquet file requires at minimum one `GetObject` call to read the footer, plus one per column chunk you need. 1,400 files × 2-3 calls each = thousands of S3 API calls just to answer "what was the spread at 8pm?" S3 charges per request and each call has ~50-100ms latency.

**2. Row group statistics become useless.** A 60-second file has one row group covering a tiny time window. The query engine opens every file just to check if it overlaps your time range, because it can't know without reading the footer. A single compacted file with 3 row groups lets the engine read one footer and skip 2/3 of the data.

**3. Athena/Spark planning overhead.** Before executing, the query planner must list all files in the partition (`ListObjectsV2`), read every footer, and build an execution plan. With 1,400 files this takes seconds before the query even starts. With 1 file it's instant.

**4. No effective dictionary encoding.** Dictionary encoding compresses well when the dictionary is built over many rows. A 60-second file might only see 20 of 500 tickers — the dictionary is small and the index column barely compresses. A full-day file sees all 500 tickers and the RLE on the indices compresses dramatically.

## The numbers

| | 1,400 small files | 1 compacted file |
|---|---|---|
| S3 API calls per query | ~3,000+ | ~5 |
| Footer reads | 1,400 | 1 |
| Athena planning time | 5-15 seconds | <1 second |
| Row group skip effectiveness | None (1 group per file) | High (skip 2/3 of data on time-range filter) |
| Dictionary encoding ratio | Poor (20 tickers per file) | Excellent (500 tickers, heavy RLE) |

## Why not just flush less often?

We do — the `min_rows=1000` guard and `flush_seconds=300` default help. But there's a fundamental tension:

- **Durability wants frequent flushes.** Data sitting in memory is lost if the process crashes. The buffer is the durability gap.
- **Query performance wants large files.** Fewer, bigger files query faster.

Compaction resolves this by decoupling the two concerns: flush frequently for durability (small files are fine for archival), then compact daily for query performance.

## When to compact

Run compaction daily for yesterday's date. Never compact today — the live writer is still producing files. The compaction script enforces this with a safety check.

```bash
# Daily cron, e.g., at 02:00 UTC
python -m v2.scripts.compact_silver --date $(date -u -d 'yesterday' +%Y-%m-%d)
```
