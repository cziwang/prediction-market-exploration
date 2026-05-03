# v2/app/services

Async S3 writers for the two storage layers. Both are async context managers that buffer in memory and flush on time/size thresholds or shutdown.

## bronze_writer.py

Writes raw WS frames as gzip-compressed JSONL to S3. Bronze is the authoritative archive — every byte from Kalshi is preserved exactly as received.

- **Writes to:** `s3://prediction-markets-data/bronze/kalshi_ws/{channel}/{Y}/{M}/{D}/{H}/{uuid}.jsonl.gz`
- **Flush triggers:** 5 MB uncompressed or 60 seconds elapsed

## silver_writer.py

Writes typed events and depth rows as Parquet with v=3 optimizations to S3.

- **Writes to:** `s3://prediction-markets-data/silver/kalshi_ws/{EventType}/date={YYYY-MM-DD}/v=3/part-{uuid}.parquet`
- **Flush triggers:** 300 seconds (default), 1000-row minimum guard

Two emit paths:
- `emit(event: Event)` — for Event dataclasses (TradeEvent, BookInvalidated). Converted via `asdict()` + timestamp conversion at flush time.
- `emit_row(type_name: str, row: dict)` — for pre-formatted dicts (OrderBookDepth). Timestamps already in nanoseconds, no conversion needed.

Optimizations: explicit `pa.Schema` per event type, dictionary encoding for strings, sorted by `t_receipt_ns`, row_group_size=100K, ZSTD compression.
