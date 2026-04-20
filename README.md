# prediction-market-exploration

Collecting NBA game data and Kalshi prediction market data for analysis and backtesting quantitative sports betting strategies.

## Architecture

Two parallel tracks running against the same S3 bucket.

**Batch fetchers** — one-shot historical pulls:

```
         Data Sources                          Storage
    ┌───────────────────┐
    │  cdn.nba.com      │──┐   ┌──────────────────────┐
    │  (REST)           │  ├──>│  S3 (raw JSON)       │
    └───────────────────┘  │   │  prediction-markets- │
    ┌───────────────────┐  │   │  data bucket          │
    │  Kalshi API       │──┘   └──────────────────────┘
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
| `cdn.nba.com` | Schedule, scoreboard, odds, box scores, PBP | REST (no auth) | `app/clients/nba_cdn.py`, `scripts/live/nba_cdn/` |
| Kalshi (live REST) | Current markets, orderbook | `kalshi-python` SDK | `app/clients/kalshi_sdk.py` |
| Kalshi (live WS) | Orderbook snapshots + deltas | authenticated WebSocket | `scripts/live/kalshi_ws/` |
| Kalshi (historical) | Settled markets, trades, candlesticks | REST | `app/clients/kalshi_rest.py` |

### S3 data layout

```
s3://prediction-markets-data/
├── nba_cdn/                    # cdn.nba.com (batch)
│   ├── schedule/
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

# Configure AWS CLI (for S3 storage). On EC2, an IAM instance profile
# replaces this — boto3 picks credentials up automatically.
aws configure
```

### Kalshi credentials

Optional for the REST batch fetchers (higher rate limits), **required**
for `scripts/live/kalshi_ws/` — Kalshi's WebSocket needs an
authenticated handshake even for public market channels.

1. Create an API key in Kalshi's settings UI; download the `.pem`
   private key Kalshi only shows you once.
2. Put the `.pem` outside any git-tracked directory (`keys/` in the
   repo is gitignored) and `chmod 600` it.
3. Create `.env` in the repo root with:

   ```
   KALSHI_API_KEY_ID=<uuid from kalshi>
   KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi-private-key.pem
   ```

   Use an absolute path for `KALSHI_PRIVATE_KEY_PATH`. `.env` is
   gitignored; `chmod 600` it too.

## Usage

```bash
source .venv/bin/activate

# --- NBA data (cdn.nba.com) ---
python -m scripts.nba_cdn.fetch_schedule                       # full season schedule (current season only)
python -m scripts.nba_cdn.fetch_scoreboard                     # today's scoreboard
python -m scripts.nba_cdn.fetch_odds                           # today's odds
python -m scripts.nba_cdn.fetch_boxscores                      # today's box scores
python -m scripts.nba_cdn.fetch_boxscores --season 2025-26     # backfill full season
python -m scripts.nba_cdn.fetch_play_by_play                   # today's PBP
python -m scripts.nba_cdn.fetch_play_by_play --season 2025-26  # backfill full season

# --- Kalshi historical data ---
python -m scripts.kalshi.fetch_historical_markets              # all NBA series (run first)
python -m scripts.kalshi.fetch_historical_trades --workers 4
python -m scripts.kalshi.fetch_historical_candlesticks --workers 4 --interval 60

# --- Live streaming ---
python -m scripts.infra.smoke_test                             # end-to-end test of BronzeWriter + SilverWriter
python -m scripts.live.nba_cdn                                 # NBA CDN → bronze (scoreboard, odds, live PBP, boxscore)
python -m scripts.live.kalshi_ws                               # Kalshi WS → bronze (KXNBAGAME orderbook)
```

For long-running deployment of the live ingesters under `systemd`,
see [`docs/live-nba-cdn-service.md`](docs/live-nba-cdn-service.md) and
[`docs/live-kalshi-ws-service.md`](docs/live-kalshi-ws-service.md).

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

- **NBA**: All data from cdn.nba.com. Schedule endpoint serves current season only; PBP/boxscore endpoints work for historical game IDs.
- **Kalshi**: April 2025 – February 2026. ~54k markets across all series. Historical cutoff is ~Feb 16, 2026.
