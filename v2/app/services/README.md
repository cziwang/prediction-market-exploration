# v2/app/services

Async S3 writers for the two storage layers. Both are async context managers that buffer in memory and flush on time/size thresholds or shutdown.

## bronze_writer.py

Writes raw WS frames as gzip-compressed JSONL to S3. Bronze is the authoritative archive — every byte from Kalshi is preserved exactly as received. Format is intentionally simple (not Parquet) because the raw frames have variable structure.

- **Writes to:** `s3://prediction-markets-data/bronze/kalshi_ws/{channel}/{Y}/{M}/{D}/{H}/{uuid}.jsonl.gz`
- **Flush triggers:** 5 MB uncompressed or 60 seconds elapsed
- Each record includes `t_receipt`, `conn_id`, and the full raw `frame`

## silver_writer.py

Writes typed events as Parquet with v=3 optimizations to S3. This is the queryable layer — what Athena, notebooks, and the replay engine read from.

- **Writes to:** `s3://prediction-markets-data/silver/kalshi_ws/{EventType}/date={YYYY-MM-DD}/v=3/part-{uuid}.parquet`
- **Flush triggers:** 300 seconds (default) or 60 seconds (live mode), with a 1000-row minimum guard

v2 improvements over v1:
- Explicit `pa.Schema` per event type (no inference)
- `t_receipt_ns` as `int64` nanoseconds (not `float64` seconds)
- Dictionary encoding for string columns (`market_ticker`, `side`, etc.)
- Rows sorted by `t_receipt_ns` before writing
- `row_group_size=100,000` for predicate pushdown
- `min_rows=1000` flush guard to reduce tiny-file count
