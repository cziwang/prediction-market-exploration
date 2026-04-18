# CLAUDE.md

## Project overview

Prediction market exploration — collecting NBA game data and Kalshi prediction market data for analysis and backtesting quantitative sports betting strategies.

Currently in the **data collection phase**. No modeling, backtesting, or trading yet.

## Project structure

```
app/
├── clients/
│   ├── kalshi_sdk.py          # Kalshi live API via official Python SDK
│   ├── kalshi_rest.py         # Kalshi historical API via raw HTTP (SDK doesn't support /historical/*)
│   ├── nba_stats.py           # NBA historical data via nba_api SDK (stats.nba.com)
│   └── nba_cdn.py             # NBA live data via REST (cdn.nba.com, no auth needed)
├── services/
│   └── s3_raw.py              # Read/write raw JSON to S3, deterministic keys for dedup
├── core/
│   └── config.py              # S3_BUCKET, SERIES constants, env vars via dotenv
scripts/
├── nba_stats/                 # Historical NBA data (stats.nba.com)
│   ├── fetch_games.py         # Season game results → nba/games/
│   └── fetch_play_by_play.py  # Play-by-play per game → nba/play_by_play/
├── nba_cdn/                   # Live NBA data (cdn.nba.com)
│   ├── fetch_scoreboard.py    # Today's scoreboard → nba_cdn/scoreboard/
│   ├── fetch_odds.py          # Today's odds → nba_cdn/odds/
│   ├── fetch_boxscores.py     # Today's box scores → nba_cdn/boxscore/
│   └── fetch_play_by_play.py  # Today's PBP → nba_cdn/play_by_play/
├── kalshi/                    # Kalshi historical data
│   ├── fetch_historical_markets.py       # All NBA series markets → kalshi/historical_markets/
│   ├── fetch_historical_trades.py        # Trades per market → kalshi/historical_trades/
│   └── fetch_historical_candlesticks.py  # OHLC per market → kalshi/historical_candlesticks/{interval}m/
notebooks/
└── nba_eda.ipynb              # EDA notebook for NBA game data
```

## S3 data layout

All raw data stored in `s3://prediction-markets-data/` with this prefix structure:

```
nba/                           # From nba_api SDK (stats.nba.com)
  games/season_2024-25.json
  play_by_play/{game_id}.json

nba_cdn/                       # From cdn.nba.com REST
  scoreboard/{date}.json
  odds/{date}.json
  boxscore/{game_id}.json
  play_by_play/{game_id}.json

kalshi/                        # From Kalshi REST API
  historical_markets/{series}.json
  historical_trades/{ticker}.json
  historical_candlesticks/{interval}m/{ticker}.json
```

Source prefix (`nba/` vs `nba_cdn/` vs `kalshi/`) differentiates data origin.

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

- **Idempotent fetches**: All scripts check S3 for existing keys before fetching. Safe to re-run.
- **Deterministic S3 keys**: Same API call overwrites same key, no duplicates.
- **Rate limit retry**: `kalshi_rest.py` retries on 429 with exponential backoff.
- **Thread pool**: Trades and candlesticks scripts use `ThreadPoolExecutor` for concurrent fetches (`--workers N`).
- **Two rows per game**: NBA game data has one row per team per game.
- **Two markets per game**: Kalshi win/loss markets have one market per team (linked by `event_ticker`).

## Common commands

```bash
source .venv/bin/activate

# NBA historical data
python -m scripts.nba_stats.fetch_games
python -m scripts.nba_stats.fetch_play_by_play

# NBA live data
python -m scripts.nba_cdn.fetch_scoreboard
python -m scripts.nba_cdn.fetch_odds
python -m scripts.nba_cdn.fetch_boxscores
python -m scripts.nba_cdn.fetch_play_by_play

# Kalshi historical data (markets first, then trades + candlesticks)
python -m scripts.kalshi.fetch_historical_markets
python -m scripts.kalshi.fetch_historical_trades --workers 4
python -m scripts.kalshi.fetch_historical_candlesticks --workers 4 --interval 60
```

## Data coverage

- **NBA game data**: 2024-25 season (preseason + regular + playoffs + all-star). ~1,401 games, ~690k plays.
- **Kalshi historical markets**: April 2025 – February 2026 (when Kalshi first launched NBA markets through the historical cutoff). ~54k markets across all series.
- **Kalshi historical cutoff**: ~Feb 16, 2026. Data before this date is in `/historical/*` endpoints. After is in live endpoints.

## Environment

- Python 3.13, virtualenv at `.venv/`
- AWS credentials via `~/.aws/credentials` (not in .env)
- `.env` has `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` (optional, for higher rate limits)
- `S3_BUCKET` defaults to `prediction-markets-data` if not in .env
