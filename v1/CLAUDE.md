# v1 — Live MM Strategy (deprecated)

**Status:** Deployed on EC2, still running. No new development — active work is in `v2/`.

## What's here

- **Batch fetchers** — one-shot scripts pulling historical Kalshi data to S3
- **Live ingester** — WS → bronze (gzip-JSONL) + transform + silver (Parquet v=2)
- **MM strategy** — paper trading on KXNBAPTS (player points), deployed via systemd

## Key docs

- `docs/data-flow.md` — pipeline architecture
- `docs/strategy-kalshi-mm.md` — MM strategy design
- `docs/deploy-mm-paper.md` — Phase 1 deployment

## Commands

```bash
source .venv/bin/activate
python -m pytest v1/tests/ -v                              # all tests (66)
MM_ENABLED=1 python -m scripts.live.kalshi_ws              # paper trading
python -m scripts.mm_stats                                 # paper trading dashboard
```

## Gotchas

- v1 writes silver as `v=2` with float timestamps and inferred schemas. v2 writes `v=3` with int64 ns, explicit schemas, and dictionary encoding.
- Imports use `from app.` not `from v1.app.` — the live EC2 deployment predates the v1/ move.
- `SILVER_VERSION = 2` in `app/core/config.py`. Do not bump — v2 owns v=3.
