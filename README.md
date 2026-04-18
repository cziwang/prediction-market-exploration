# prediction-market-exploration

Collecting NBA game data and Kalshi prediction market data for analysis and backtesting quantitative sports betting strategies.

## Architecture

Two parallel tracks running against the same S3 bucket.

**Batch fetchers** — one-shot historical pulls:

```
         Data Sources                          Storage
    ┌───────────────────┐
    │  stats.nba.com    │──┐
    │  (nba_api SDK)    │  │
    └───────────────────┘  │
    ┌───────────────────┐  │   ┌──────────────────────┐
    │  cdn.nba.com      │──┼──>│  S3 (raw JSON)       │
    │  (REST)           │  │   │  prediction-markets- │
    └───────────────────┘  │   │  data bucket          │
    ┌───────────────────┐  │   └──────────────────────┘
    │  Kalshi API       │──┘
    │  (SDK + REST)     │
    └───────────────────┘
```

**Live streaming** (in progress) — long-running process per source, fans each raw frame four ways in-process:

```
┌─────────────────────────────────────────────────────────────┐
│  Live process (one per source)                              │
│    ingester (WS / REST poll)                                │
│         │                                                   │
│         ├──▶ BronzeWriter  ──▶ s3://…/bronze/ (gzip JSONL)  │
│         ▼                                                   │
│    transform() ──▶ strategy.on_event() (in-process)         │
│         │                                                   │
│         └──▶ SilverWriter  ──▶ s3://…/silver/ (Parquet)     │
└─────────────────────────────────────────────────────────────┘
```

The key property: `transform()` runs exactly once per event, and its output is simultaneously what the strategy sees live and what silver stores for backtests. Structural parity. Full design in [`docs/data-flow.md`](docs/data-flow.md).

### Data flow

1. **Batch fetchers** — clients call external APIs, raw JSON written to S3 with deterministic keys (idempotent; re-runs skip existing data).
2. **Live streaming** — per-source process ingests raw frames, writes gzipped JSONL to `bronze/`, runs `transform()` inline, feeds the strategy, and serializes typed events to Parquet under `silver/`. Bronze is authoritative; silver is rebuildable.

### Data sources

| Source | What | Interface | Client |
|---|---|---|---|
| `stats.nba.com` | Game results, play-by-play | `nba_api` SDK | `app/clients/nba_stats.py` |
| `cdn.nba.com` | Live scoreboard, odds, box scores | REST (no auth) | `app/clients/nba_cdn.py` |
| Kalshi (live) | Current markets, orderbook | `kalshi-python` SDK | `app/clients/kalshi_sdk.py` |
| Kalshi (historical) | Settled markets, trades, candlesticks | REST | `app/clients/kalshi_rest.py` |

### S3 data layout

```
s3://prediction-markets-data/
├── nba/                        # stats.nba.com (batch)
│   ├── games/
│   └── play_by_play/
├── nba_cdn/                    # cdn.nba.com (batch)
│   ├── scoreboard/
│   ├── odds/
│   ├── boxscore/
│   └── play_by_play/
├── kalshi/                     # Kalshi REST API (batch)
│   ├── historical_markets/
│   ├── historical_trades/
│   └── historical_candlesticks/
│       ├── 1m/
│       ├── 60m/
│       └── 1440m/
├── bronze/                     # Live raw frames (gzip JSONL)
│   └── {source}/{channel}/YYYY/MM/DD/HH/{uuid}.jsonl.gz
└── silver/                     # Live typed events (Parquet)
    └── {source}/{EventType}/date=YYYY-MM-DD/v=N/part-{uuid}.parquet
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure AWS CLI (for S3 storage)
aws configure

# (Optional) Add Kalshi API keys for higher rate limits
cp .env.example .env  # edit with your key ID and PEM path
```

## Usage

```bash
source .venv/bin/activate

# --- NBA historical data (stats.nba.com) ---
python -m scripts.nba_stats.fetch_games                        # 2024-25 season
python -m scripts.nba_stats.fetch_games --season 2023-24       # specific season
python -m scripts.nba_stats.fetch_play_by_play                 # PBP for all games

# --- NBA live data (cdn.nba.com) ---
python -m scripts.nba_cdn.fetch_scoreboard
python -m scripts.nba_cdn.fetch_odds
python -m scripts.nba_cdn.fetch_boxscores
python -m scripts.nba_cdn.fetch_play_by_play

# --- Kalshi historical data ---
python -m scripts.kalshi.fetch_historical_markets              # all NBA series (run first)
python -m scripts.kalshi.fetch_historical_trades --workers 4
python -m scripts.kalshi.fetch_historical_candlesticks --workers 4 --interval 60

# --- Live streaming ---
python -m scripts.infra.smoke_test                             # end-to-end test of BronzeWriter + SilverWriter
```

## Kalshi NBA series

| Series | Type | Markets |
|---|---|---|
| `KXNBAGAME` | Win/loss | 1,902 |
| `KXNBATOTAL` | Total points O/U | 9,044 |
| `KXNBASPREAD` | Point spread | 8,923 |
| `KXNBAPTS` | Player points | 8,876 |
| `KXNBAREB` | Player rebounds | 8,481 |
| `KXNBA3PT` | Player threes | 7,804 |
| `KXNBAAST` | Player assists | 6,385 |
| `KXNBASTL` | Player steals | 1,656 |
| `KXNBABLK` | Player blocks | 1,070 |
| `KXNBA` | Finals winner | 30 |
| `KXNBASERIES` | Series winner | 24 |
| `KXNBAPLAYOFF` | Playoff qualifier | 23 |
| `KXNBAALLSTAR` | All-Star game | 4 |

## Data coverage

- **NBA**: 2024-25 season (preseason + regular + playoffs). ~1,401 games, ~690k plays.
- **Kalshi**: April 2025 – February 2026. ~54k markets across all series. Historical cutoff is ~Feb 16, 2026.
