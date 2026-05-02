# prediction-market-exploration

Kalshi prediction market data collection, backtesting, and live market making.

## Repo layout

- `v1/` — Live MM strategy + batch fetchers (deployed on EC2, being deprecated)
- `v2/` — Data infrastructure rebuild: optimized storage, query catalog, replay engine

## Environment

- Python 3.12, virtualenv at `.venv/`
- AWS credentials via `~/.aws/credentials` or EC2 IAM role
- `.env` requires `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`
- S3 bucket: `prediction-markets-data`

## Key rules

- **v1/ and v2/ are fully independent.** Never import, reference, or share code between them unless explicitly asked. Treat them as separate repos. v1 is being deprecated and will eventually be deleted.
- Integer cents for all prices and sizes — no floats for money
- Bronze (gzip-JSONL) is the authoritative archive. Silver is rebuildable from bronze.
- Active development is in `v2/`. See `v2/CLAUDE.md` for current context.

## Commands

```bash
source .venv/bin/activate
python -m pytest v2/tests/ -v       # v2 tests (10)
python -m pytest v1/tests/ -v       # v1 tests (66)
```
