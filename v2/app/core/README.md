# v2/app/core

Shared configuration constants.

## config.py

- `S3_BUCKET` — target S3 bucket, defaults to `prediction-markets-data` (overridable via `.env`)
- `SILVER_VERSION = 3` — partition version for silver Parquet files. All v2 writes land in `v=3/` partitions. Bump this on breaking schema changes so old and new data coexist.
