# prediction-market-exploration

Kalshi prediction market data + trading.

## Repo layout

- `nba-mm/` — Market-making infrastructure (Kalshi WS feed, data pipeline, MM strategy)
  - `nba-mm/v1/` — Live MM strategy + batch fetchers (deployed on EC2, being deprecated)
  - `nba-mm/v2/` — Data infrastructure rebuild: optimized storage, query catalog, replay engine
- `nba-edge/` — NBA model + edge trading (build predictive model, trade when we have edge)
- `market-maker/` — Full LOB reconstruction + market-making (standalone, AWS stack)

## Environment

- Python 3.12, virtualenv at `.venv/`
- AWS credentials via `~/.aws/credentials` or EC2 IAM role
- `.env` requires `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`
- S3 bucket: `prediction-markets-data`

## Key rules

- **nba-mm/, nba-edge/, and market-maker/ are all independent.** Don't share code between them unless explicitly asked.
- **nba-mm/v1/ and nba-mm/v2/ are independent.** v1 is being deprecated.
- Integer cents for all prices and sizes — no floats for money
- Bronze (gzip-JSONL) is the authoritative archive. Silver is rebuildable from bronze.
- Active MM development is in `nba-mm/v2/`. See `nba-mm/v2/CLAUDE.md` for context.

## Commands

```bash
source .venv/bin/activate
python -m pytest nba-mm/v2/tests/ -v       # v2 tests
python -m pytest nba-mm/v1/tests/ -v       # v1 tests
```
