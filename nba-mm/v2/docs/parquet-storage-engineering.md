# Parquet Storage Engineering

How the v2 silver layer is optimized, and why each decision matters.

---

## Parquet file structure

```
Parquet File
  └─ Row Group (100,000 rows each)
       └─ Column Chunk (one per column, stored contiguously)
            └─ Page (~1 MB, unit of compression)
  └─ Footer (schema + per-row-group min/max stats)
```

Columnar layout means a query selecting 4 of 6 columns only reads those 4 column chunks. The others are never touched.

---

## Dictionary encoding

Replaces repeated strings with integer IDs. The dictionary is stored once per column chunk inside the file — no external lookup table.

```
Dictionary:  {0: "KXNBAPTS-JOKIC", 1: "KXNBAPTS-LEBRON", ...}
Data:        [0, 0, 1, 0, 2, 1, 0, ...]   (int16 per row)
```

```python
("market_ticker", pa.dictionary(pa.int16(), pa.utf8()))
```

~500 unique tickers, 300k rows/day: **~9 MB → ~615 KB (93% savings)** for this column alone. Readers (Polars, DuckDB, Athena) decode it transparently as strings.

Data pages also use RLE on the indices — 50 consecutive identical tickers compress to `(id, 50)`.

---

## Delta encoding for timestamps

Store differences between consecutive values instead of absolutes. Only works when sorted.

```
Absolute:  1714500000000000000, 1714500000001234000, 1714500000002567000
Deltas:    1714500000000000000, 1234000, 1333000    (3-4 bytes vs 8)
```

This is why we use **int64 nanoseconds** instead of float64 seconds — float differences are noisy and don't compress. Same 8 bytes raw, but int64 delta-encodes cleanly.

---

## ZSTD compression

Applied *after* encoding. The layers are multiplicative:

```
Raw values → Encoding (dictionary/delta/RLE) → ZSTD → bytes on disk
```

Dictionary might reduce 15x, ZSTD another 3x = ~45x total. ZSTD beats Snappy 2.5-3.5x on ratio with negligible CPU difference.

---

## Predicate pushdown

Each row group's footer stores min/max per column. Query engines read the footer first and skip row groups that don't overlap the filter.

**Requires sorted data.** If timestamps are jumbled, every row group spans the full range and nothing gets skipped. Sorting by `t_receipt_ns` ensures contiguous time windows per row group.

---

## Hive-style partitioning

```
silver/.../OrderBookUpdate/date=2026-04-30/v=3/part-abc.parquet
```

`WHERE date = '2026-04-30'` prunes all other date directories without opening any files. We don't partition by ticker — 500 tickers would create 500 tiny files per date, killing S3 list performance and row-group statistics.

Rule: partition on **low-cardinality, high-selectivity** columns (date). Use predicate pushdown for high-cardinality columns (ticker).

---

## Partition projection (Athena)

Tells Athena to generate partition paths mathematically instead of listing S3 or querying Glue metadata. No `MSCK REPAIR TABLE`, no `ADD PARTITION` — new dates are queryable immediately.

---

## How they compose

Query: "JOKIC order book updates, April 30, 8:00-8:05 PM"

1. **Partition pruning** — skip all dates except Apr 30
2. **Column pruning** — read only the 4 selected columns
3. **Row group skip** — footer stats show only 1 of 3 row groups overlaps the time range
4. **Dictionary filter** — find JOKIC's index, filter rows
5. **ZSTD decompress** — only the surviving pages

Result: ~50 KB read from S3 instead of ~10 MB. Each layer is multiplicative.
