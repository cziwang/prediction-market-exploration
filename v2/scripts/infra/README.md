# v2/scripts/infra

AWS infrastructure setup scripts. Idempotent — safe to re-run.

## setup_glue_catalog.py

Creates the AWS Glue Data Catalog and Athena workgroup for querying silver Parquet data with SQL.

- Creates `prediction_markets` Glue database
- Creates 8 tables (one per silver event type) with partition projection on `date` + `v`
- Creates `prediction-markets` Athena workgroup with 10 GB scan cutoff
- Re-running updates existing table definitions (useful after schema changes)

```bash
python -m v2.scripts.infra.setup_glue_catalog --dry-run
python -m v2.scripts.infra.setup_glue_catalog
```

## delete_silver_v2.py

Deletes all silver v=1 and v=2 files from S3. These are fully superseded by v=3 (backfilled from bronze). Lists all files with sizes, asks for confirmation before deleting.

```bash
python -m v2.scripts.infra.delete_silver_v2 --dry-run
python -m v2.scripts.infra.delete_silver_v2
```
