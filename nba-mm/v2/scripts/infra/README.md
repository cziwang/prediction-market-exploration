# v2/scripts/infra

AWS infrastructure setup scripts. Idempotent — safe to re-run.

## setup_glue_catalog.py

Creates the AWS Glue Data Catalog and Athena workgroup for querying silver Parquet data.

- Database: `prediction_markets`
- 9 silver tables (incl. `order_book_depth`) + 1 reference table (`market_metadata`)
- Partition projection: `date` (string) + `v` (integer, currently 3)
- Workgroup: `prediction-markets` with 10 GB scan cutoff

```bash
python -m v2.scripts.infra.setup_glue_catalog
```
