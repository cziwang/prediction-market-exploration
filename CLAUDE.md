# CLAUDE.md

## Project overview

Prediction market exploration — collecting NBA game data and Kalshi prediction market data for analysis and backtesting quantitative sports betting strategies.

Two parallel tracks:
- **Batch fetchers** (existing) — one-shot scripts that pull historical NBA + Kalshi data into `s3://prediction-markets-data/{nba_cdn,kalshi}/`.
- **Live streaming** (in progress) — long-running per-source processes that fan raw frames to bronze (gzip-JSONL on S3) + a transform + a strategy + silver (Parquet on S3), all in-process. Full design in [`docs/data-flow.md`](docs/data-flow.md).

## Project structure

```
app/
├── clients/
│   ├── kalshi_sdk.py          # Kalshi live API via official Python SDK
│   ├── kalshi_rest.py         # Kalshi historical API via raw HTTP (SDK doesn't support /historical/*)
│   └── nba_cdn.py             # NBA data via REST (cdn.nba.com, no auth needed)
├── services/
│   ├── s3_raw.py              # Read/write raw JSON to S3, deterministic keys for dedup (batch)
│   ├── bronze_writer.py       # Async batched gzip-JSONL writer → bronze/{source}/{channel}/... (live)
│   └── silver_writer.py       # Async batched Parquet writer → silver/{source}/{EventType}/... (live)
├── events.py                  # Typed event dataclasses (ScoreEvent, OrderBookUpdate, ...) + Event union
├── transforms/                # (planned) pure raw→typed transforms, one per source
├── strategy/                  # (planned) consumes events.py; never touches raw JSON
└── core/
    └── config.py              # S3_BUCKET, SERIES, SILVER_VERSION, env vars via dotenv
scripts/
├── nba_cdn/                   # NBA data (cdn.nba.com) — batch + historical
│   ├── fetch_schedule.py      # Full season schedule → nba_cdn/schedule/
│   ├── fetch_scoreboard.py    # Today's scoreboard → nba_cdn/scoreboard/
│   ├── fetch_odds.py          # Today's odds → nba_cdn/odds/
│   ├── fetch_boxscores.py     # Box scores (today or --season) → nba_cdn/boxscore/
│   └── fetch_play_by_play.py  # PBP (today or --season) → nba_cdn/play_by_play/
├── kalshi/                    # Kalshi historical data
│   ├── fetch_historical_markets.py       # All NBA series markets → kalshi/historical_markets/
│   ├── fetch_historical_trades.py        # Trades per market → kalshi/historical_trades/
│   └── fetch_historical_candlesticks.py  # OHLC per market → kalshi/historical_candlesticks/{interval}m/
├── live/                      # (planned) live ingester+transformer+writer, one process per source
│   ├── kalshi_ws.py           # Kalshi WS → bronze + strategy + silver
│   └── nba_cdn.py             # NBA polling → bronze + strategy + silver
├── materialize/               # (planned) rebuild silver from bronze after transform changes
└── infra/
    └── smoke_test.py          # End-to-end test of BronzeWriter + SilverWriter against real S3
notebooks/
└── nba_eda.ipynb              # EDA notebook for NBA game data
```

## S3 data layout

All data stored in `s3://prediction-markets-data/`:

```
# Batch fetchers — raw JSON, deterministic keys
nba_cdn/                       # From cdn.nba.com REST
  schedule/season_{year}.json
  scoreboard/{date}.json
  odds/{date}.json
  boxscore/{game_id}.json
  play_by_play/{game_id}.json

kalshi/                        # From Kalshi REST API
  historical_markets/{series}.json
  historical_trades/{ticker}.json
  historical_candlesticks/{interval}m/{ticker}.json

# Live streaming — written by BronzeWriter / SilverWriter
bronze/{source}/{channel}/YYYY/MM/DD/HH/{uuid}.jsonl.gz
silver/{source}/{EventType}/date=YYYY-MM-DD/v=N/part-{uuid}.parquet
```

Top-level prefix differentiates origin: `nba_cdn/`, `kalshi/` for batch; `bronze/`, `silver/` for live.

## Kalshi NBA series

```
KXNBAGAME      — win/loss
KXNBASPREAD    — point spread
KXNBATOTAL     — total points over/under
KXNBAPTS       — player points
KXNBAREB       — player rebounds
KXNBAAST       — player assists
KXNBA3PT       — player threes
KXNBABLK       — player blocks
KXNBASTL       — player steals
KXNBA          — NBA Finals winner
KXNBASERIES    — playoff series winner
KXNBAPLAYOFF   — playoff qualifier
KXNBAALLSTAR   — all-star game
```

The series list is defined in `scripts/kalshi/fetch_historical_markets.py::ALL_NBA_SERIES`.

## Key patterns

### Batch fetchers
- **Idempotent fetches**: All scripts check S3 for existing keys before fetching. Safe to re-run.
- **Deterministic S3 keys**: Same API call overwrites same key, no duplicates.
- **Rate limit retry**: `kalshi_rest.py` retries on 429 with exponential backoff.
- **Thread pool**: Trades and candlesticks scripts use `ThreadPoolExecutor` for concurrent fetches (`--workers N`).
- **Two markets per game**: Kalshi win/loss markets have one market per team (linked by `event_ticker`).

### Live streaming
- **One live process per source**: each owns its wire connection, BronzeWriter, transform, strategy, SilverWriter. A Kalshi reconnect storm can't restart the NBA poller.
- **Single transform execution site**: `transform()` runs once, inline in the live process. Its output simultaneously feeds the strategy and is serialized to silver. Backtest ↔ live parity is structural, not disciplinary.
- **Bronze is authoritative**: raw bytes on S3 are the permanent archive. Silver is rebuildable from bronze via `scripts/materialize/` whenever the transform changes.
- **Async writers**: `BronzeWriter` and `SilverWriter` are async context managers that buffer in memory, flush on size/time, and drain on shutdown. `emit()` is fire-and-forget; the WS reader never blocks on S3.
- **Version-pinned silver**: `silver/.../v=N/` segment (`SILVER_VERSION` in `app/core/config.py`) pins the transform version. Breaking schema changes bump `N` and land in a new partition; notebooks pin to a version.

## Common commands

```bash
source .venv/bin/activate

# NBA data (cdn.nba.com)
python -m scripts.nba_cdn.fetch_schedule                            # full season schedule
python -m scripts.nba_cdn.fetch_scoreboard                          # today's scoreboard
python -m scripts.nba_cdn.fetch_odds                                # today's odds
python -m scripts.nba_cdn.fetch_boxscores                           # today's box scores
python -m scripts.nba_cdn.fetch_boxscores --season 2025-26          # backfill season
python -m scripts.nba_cdn.fetch_play_by_play                        # today's PBP
python -m scripts.nba_cdn.fetch_play_by_play --season 2025-26       # backfill season

# Kalshi historical data (markets first, then trades + candlesticks)
python -m scripts.kalshi.fetch_historical_markets
python -m scripts.kalshi.fetch_historical_trades --workers 4
python -m scripts.kalshi.fetch_historical_candlesticks --workers 4 --interval 60

# Live streaming — smoke test for the BronzeWriter + SilverWriter path
python -m scripts.infra.smoke_test
```

## Data coverage

- **NBA game data**: All data sourced from cdn.nba.com. Schedule endpoint only serves current season.
- **Kalshi historical markets**: April 2025 – February 2026 (when Kalshi first launched NBA markets through the historical cutoff). ~54k markets across all series.
- **Kalshi historical cutoff**: ~Feb 16, 2026. Data before this date is in `/historical/*` endpoints. After is in live endpoints.

## Environment

- Python 3.12, virtualenv at `.venv/`
- AWS credentials via `~/.aws/credentials` or an EC2 IAM role
- `.env` has `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` (optional, for higher rate limits)
- `S3_BUCKET` defaults to `prediction-markets-data` if not in .env
- `SILVER_VERSION` in `app/core/config.py` (currently `1`) pins the transform version written to silver
