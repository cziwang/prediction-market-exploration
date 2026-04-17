# prediction-market-exploration

Historical NBA game data and Kalshi prediction market data for backtesting
quantitative sports betting strategies.

## Architecture

```
                   Data Sources                        Storage                    Query Layer
              ┌──────────────────┐
              │  NBA Stats API   │──┐
              │  (game results)  │  │
              └──────────────────┘  │   ┌─────────────────┐
                                    ├──>│  S3 (raw JSON)  │  immutable, replay-able
              ┌──────────────────┐  │   └────────┬────────┘
              │  Kalshi SDK      │──┘            │
              │  (markets/OHLC)  │          ingest + normalize
              └──────────────────┘              │
                                    ┌───────────▼──────────┐
                                    │  Postgres (RDS)      │──> FastAPI
                                    │  normalized tables   │──> strategy / backtest
                                    └──────────────────────┘
```

### Data flow

1. **Fetch** — clients call external APIs and return raw responses
2. **Store raw** — raw JSON is written to S3 with deterministic keys (same call twice overwrites, no duplicates)
3. **Normalize** — ingest services map API-specific fields to a canonical schema
4. **Write** — repositories upsert normalized rows to the database (primary key dedup)

Raw data in S3 is never mutated. If the normalization logic changes, the
normalized tables can be rebuilt by replaying from S3.

```
app/clients/nba.py           fetch_season_games("2024-25")
        │                              │
        │ raw rows                     │ raw JSON
        ▼                              ▼
app/services/nba_ingest.py   app/services/s3_raw.py
        │                      → s3://prediction-markets-data/nba/games/season_2024-25.json
        │ normalized dicts
        ▼
app/repositories/games.py
        → INSERT OR REPLACE into games table
```

### Data sources

| Source | What | Endpoint | Client |
|---|---|---|---|
| NBA Stats API | Game results, scores, box scores | `stats.nba.com` | `app/clients/nba.py` |
| Kalshi SDK | Prediction market prices, OHLC candles | `api.elections.kalshi.com` | `app/clients/kalshi.py` |

### Storage

| Layer | Technology | Purpose |
|---|---|---|
| Raw | S3 (`prediction-markets-data`) | Immutable JSON, one file per API call, deterministic keys |
| Normalized | Postgres (RDS) / SQLite (local dev) | Canonical schema, queryable, strategy reads from here |
| Local dev | SQLite (`data/kalshi_nba.db`) | Drop-in local replacement, same schema |

## Project layout

```
app/
├── main.py                    # FastAPI app, wires routers
├── api/routes/
│   ├── markets.py             # /markets endpoints
│   ├── events.py              # /events endpoints
│   ├── candlesticks.py        # /markets/{ticker}/candlesticks
│   └── analytics.py           # /analytics/* endpoints
├── clients/
│   ├── kalshi.py              # Kalshi SDK wrapper
│   └── nba.py                 # NBA stats API client
├── services/
│   ├── nba_ingest.py          # normalize NBA data, orchestrate store + write
│   └── s3_raw.py              # read/write raw JSON to S3
├── repositories/
│   └── games.py               # games table schema + upsert
├── db/
│   └── session.py             # DB connection, schema init
├── core/
│   └── config.py              # shared constants, env vars
scripts/
├── fetch_nba_games.py         # CLI: fetch NBA game results
└── fetch_nba_history.py       # CLI: fetch Kalshi market history
```

## Setup

```bash
# 1. Create virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure AWS CLI (for S3 storage)
aws configure

# 3. (Optional) Add Kalshi API keys for higher rate limits
mkdir -p keys
mv ~/Downloads/kalshi-*.pem keys/kalshi-private-key.pem
chmod 600 keys/kalshi-private-key.pem

# 4. Create .env (gitignored)
cat > .env <<EOF
KALSHI_API_KEY_ID=<your key id>
KALSHI_PRIVATE_KEY_PATH=./keys/kalshi-private-key.pem
S3_BUCKET=prediction-markets-data
EOF
```

## Usage

```bash
source .venv/bin/activate

# --- NBA game results ---
python -m scripts.fetch_nba_games                          # 2024-25 season
python -m scripts.fetch_nba_games --season 2023-24         # specific season
python -m scripts.fetch_nba_games --skip-s3                # local DB only, no S3

# --- Kalshi market data ---
python -m scripts.fetch_nba_history --csv --max-markets 5  # quick test
python -m scripts.fetch_nba_history --csv                  # all settled markets
python -m scripts.fetch_nba_history --candles --csv        # + OHLC candles

# --- API server ---
uvicorn app.main:app --reload
# Docs: http://127.0.0.1:8000/docs
```

## Schema

### `games` — NBA game results

Two rows per game (one per team). Primary key: `(game_id, team_id)`.

| column | type | notes |
|---|---|---|
| `game_id` | TEXT | NBA game ID |
| `game_date` | TEXT | game date |
| `team_abbr` | TEXT | e.g. `BOS`, `LAL` |
| `matchup` | TEXT | e.g. `BOS vs. LAL` |
| `wl` | TEXT | `W` or `L` |
| `pts` | INTEGER | points scored |
| `fg_pct` / `fg3_pct` / `ft_pct` | REAL | shooting percentages |
| `reb` / `ast` / `stl` / `blk` / `tov` | INTEGER | box score stats |
| `plus_minus` | REAL | point differential |

### `markets` — Kalshi prediction markets

One row per game-side market. Ticker format:
`KXNBAGAME-{YYMMMDD}{AWAY}{HOME}-{SIDE}`.
Each NBA game has two markets (one per team) sharing an `event_ticker`.

| column | type | notes |
|---|---|---|
| `ticker` | TEXT PK | market ticker |
| `event_ticker` | TEXT | groups the two sides of one game |
| `status` | TEXT | `finalized`, `open`, `settled`, etc. |
| `result` | TEXT | `yes` / `no` — which side won |
| `volume` | INTEGER | lifetime contract volume |
| `raw_json` | TEXT | full API payload |

### `candlesticks` — Kalshi OHLC price data

YES-side price in cents (0-100), representing implied probability.

| column | type | notes |
|---|---|---|
| `ticker` | TEXT | market ticker |
| `end_period_ts` | INTEGER | unix timestamp |
| `price_open/high/low/close` | REAL | OHLC in cents |
| `volume` | INTEGER | contracts traded in bucket |

## Planned work

- [ ] Set up RDS Postgres and migrate from SQLite
- [ ] Add Kalshi ingest service (raw S3 + normalized Postgres)
- [ ] Live data polling (real-time Kalshi orderbook + NBA game state)
- [ ] Strategy backtesting framework reading from normalized tables
- [ ] Live trading execution via Kalshi SDK
