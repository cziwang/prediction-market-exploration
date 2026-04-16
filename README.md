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

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` at the repo root:

```
KALSHI_API_KEY_ID=<your key id>
KALSHI_PRIVATE_KEY_PATH=./keys/kalshi-private-key.pem
```

Generate the key pair at <https://kalshi.com/account/profile> → API Keys.
Download the PEM and drop it at `./keys/kalshi-private-key.pem`
(or point `KALSHI_PRIVATE_KEY_PATH` elsewhere). Lock it down:

```bash
chmod 600 keys/kalshi-private-key.pem
```

The script runs without keys too — the historical endpoints are public. You
just get tighter rate limits.

## Usage

```bash
# Market metadata only (fast — one paginated list call)
python fetch_nba_history.py

# + per-minute OHLC price history for every settled game (slow — one call per market)
python fetch_nba_history.py --candles

# Coarser resolution
python fetch_nba_history.py --candles --period 60     # hourly
python fetch_nba_history.py --candles --period 1440   # daily

# Re-fetch candles already in the DB
python fetch_nba_history.py --candles --refresh-candles

# Skip market refresh, only fetch missing candles
python fetch_nba_history.py --candles --skip-markets
```

Output lives at `data/kalshi_nba.db`. Re-runs are resumeable — the `fetch_log`
table tracks which tickers already have candles for a given period.

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
| `status` | TEXT | `settled`, `open`, etc. |
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
