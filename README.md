# prediction-market-exploration

Fetch historical NBA game markets from [Kalshi](https://kalshi.com) and store them
locally in SQLite for analysis.

Pulls from the `KXNBAGAME` series (per-game winner markets) via Kalshi's public
REST API. Auth is optional — used only for higher rate limits.

## Layout

```
.env                    # your Kalshi Key ID + PEM path (gitignored)
keys/                   # drop your kalshi-private-key.pem here (gitignored)
data/                   # SQLite output (gitignored, created at runtime)
kalshi_client.py        # RSA-PSS signed HTTP client with retry + pagination
fetch_nba_history.py    # CLI entry point
requirements.txt
```

## Setup (one-time)

```bash
# 1. Create a virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. (Optional) Add Kalshi API keys for higher rate limits
#    Generate at https://kalshi.com/account/profile → API Keys
#    You'll download a PEM private key and get a Key ID.
mkdir -p keys
mv ~/Downloads/kalshi-*.pem keys/kalshi-private-key.pem
chmod 600 keys/kalshi-private-key.pem

# 3. Create .env with your Key ID (file is gitignored)
cat > .env <<EOF
KALSHI_API_KEY_ID=<paste your key id here>
KALSHI_PRIVATE_KEY_PATH=./keys/kalshi-private-key.pem
EOF
```

The script also works with **no keys** — the public market endpoints are
unauthenticated. Just skip step 2–3.

## Running

```bash
# Always activate the venv first
source .venv/bin/activate

# --- Quick test run (recommended first) ---
# Pull 5 markets + their 1-minute candlesticks, dump to CSV
python fetch_nba_history.py --candles --csv --max-markets 5

# --- Full pull ---
# All settled NBA games, metadata only (fast)
python fetch_nba_history.py --csv

# All settled NBA games + per-minute OHLC (slow — one API call per market)
python fetch_nba_history.py --candles --csv

# Coarser candlestick resolution to cut request count
python fetch_nba_history.py --candles --csv --period 60     # hourly
python fetch_nba_history.py --candles --csv --period 1440   # daily

# Re-export CSV from an existing DB (no API calls)
python fetch_nba_history.py --csv-only
```

### All flags

| flag | purpose |
|---|---|
| `--candles` | also fetch OHLC candlesticks for each settled market |
| `--period {1,60,1440}` | candlestick interval in minutes (default: 1) |
| `--csv` | after fetching, dump `markets.csv` + `candlesticks.csv` to `data/` |
| `--csv-only` | skip API calls, just re-export existing DB to CSV |
| `--max-markets N` | cap markets fetched (useful for smoke-tests) |
| `--refresh-candles` | re-fetch candles already stored for the given period |
| `--skip-markets` | skip metadata refresh, only fetch missing candles |
| `--throttle SECS` | sleep between candlestick calls (default: 0.1s) |

### Outputs

- `data/kalshi_nba.db` — SQLite, source of truth, re-runs are resumeable
- `data/markets.csv` — one row per market (minus the raw JSON blob)
- `data/candlesticks.csv` — one row per OHLC bucket

Resume behavior: the `fetch_log` table tracks which `(ticker, period)` pairs
already have candles. Re-running skips them unless `--refresh-candles` is set.

## Schema

**`markets`** — one row per game-side market. Ticker format:
`KXNBAGAME-{YYMMMDD}{AWAY}{HOME}-{SIDE}` (e.g. `KXNBAGAME-26APR14MIACHA-CHA`).
Each NBA game has two markets (one per team) sharing an `event_ticker`.

| column | type | notes |
|---|---|---|
| `ticker` | TEXT PK | market ticker |
| `event_ticker` | TEXT | groups the YES/NO sides of one game |
| `series_ticker` | TEXT | always `KXNBAGAME` here |
| `title` / `yes_sub_title` / `no_sub_title` | TEXT | human-readable |
| `open_time` / `close_time` | TEXT (ISO) | market lifecycle |
| `status` | TEXT | `finalized`, `open`, `settled`, etc. |
| `result` | TEXT | `yes` / `no` — which side won |
| `volume` | INTEGER | lifetime contract volume |
| `raw_json` | TEXT | full API payload for fields not otherwise extracted |

**`candlesticks`** — OHLC for YES price (cents, 0–100), plus bid/ask close,
volume, open interest. Bucketed by `period_min` (1, 60, or 1440).

**`fetch_log`** — tracks `(ticker, period_min)` → `(fetched_at, candle_count)`
so re-runs skip already-downloaded data.

## Example queries

```sql
-- Settled games with their winners
SELECT ticker, title, result, volume
FROM markets
WHERE status = 'settled'
ORDER BY close_time DESC
LIMIT 20;

-- Closing implied probability trajectory for one game
SELECT datetime(end_period_ts, 'unixepoch') AS t, price_close
FROM candlesticks
WHERE ticker = 'KXNBAGAME-26APR14MIACHA-CHA'
ORDER BY end_period_ts;

-- Games with the biggest in-game swings
SELECT ticker, MAX(price_high) - MIN(price_low) AS range_cents
FROM candlesticks
GROUP BY ticker
ORDER BY range_cents DESC
LIMIT 20;
```

## Notes

- Kalshi splits history between `/historical/markets` (pre-cutoff archive) and
  `/markets?status=settled` (recent). The script hits both and dedupes.
- Prices are cents (0–100), representing YES-side implied probability.
- Candlestick requests retry on 429/5xx with exponential backoff and honor
  `Retry-After`. `--throttle` adds a between-request sleep (default 0.1s).
- API base URL is `https://api.elections.kalshi.com/trade-api/v2` — despite
  the name, this serves all Kalshi markets (sports included).
