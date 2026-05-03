# market-maker

Kalshi full limit order book reconstruction and market-making infrastructure.

Based on Marriott (2026), "Reconstructing Full Limit Order Books for Kalshi from WebSocket Streams" — snapshot-anchored windowing with sequential delta replay. AWS stack (no KDB+).

## Architecture

```
Kalshi WS + REST API
        │
        ▼
   Collector (EC2 systemd service)
        │
        ├── orderbook_delta  → s3://prediction-markets-data/mm/bronze/deltas/dt=YYYY-MM-DD/
        ├── REST snapshots   → s3://prediction-markets-data/mm/bronze/snapshots/dt=YYYY-MM-DD/
        └── trades           → s3://prediction-markets-data/mm/bronze/trades/dt=YYYY-MM-DD/
        
   Reconstruction (EC2 cron job)
        │
        └── silver LOB       → s3://prediction-markets-data/mm/silver/lob/dt=YYYY-MM-DD/
        
   Query: Athena / DuckDB over silver Parquet
```

### Collection

- **WS client** subscribes to `orderbook_delta` and `trade` channels for filtered markets (default: crypto series KXBTC, KXETH, KXSOL).
- **REST poller** cycles through subscribed markets every ~15 min, fetching full orderbook snapshots via `GET /markets/{ticker}/orderbook`. These are the ground-truth anchors.
- **Market discovery** polls `GET /markets` with `series_ticker` filter to auto-discover active tickers without subscribing to the entire exchange.
- Raw messages are buffered and flushed to S3 as date-partitioned Parquet.

### Reconstruction (snapshot-anchored windowing)

For each market with ordered snapshots S1, S2, ..., Sn:
1. Define windows W_i = [ts(S_i), ts(S_{i+1}))
2. Initialize book from snapshot S_i
3. Replay all deltas within the window: `B_{t+1}(s, p) = max(0, B_t(s, p) + delta)`
4. Remove price level if quantity hits zero
5. Emit full book state (one row per active price level) after each delta
6. At window boundary: verify reconstructed state matches S_{i+1}

Output: `lob(market_ticker, ts, side, price_cents, qty)` — full depth at every tick.

### Live quoting

Separate from reconstruction. Uses `L2Book` / `BookManager` for real-time book state from WS, feeds into quoter strategy.

## Schemas

### Bronze (raw, append-only)

| Table     | Columns                                                    |
|-----------|------------------------------------------------------------|
| deltas    | market_ticker, ts, side, price_cents, delta, seq, sid      |
| snapshots | market_ticker, ts, side, price_cents, qty                  |
| trades    | market_ticker, ts, yes_price_cents, no_price_cents, count, taker_side |

### Silver (reconstructed)

| Table  | Columns                                    |
|--------|--------------------------------------------|
| lob    | market_ticker, ts, side, price_cents, qty  |
| trades | market_ticker, ts, yes_price_cents, no_price_cents, count, taker_side |

## Key rules

- Integer cents for all prices. Integer contracts for all sizes. No floats for money.
- This directory is fully independent from `/nba-edge` and `/nba-mm`. No shared code or imports.
- Bronze is the authoritative archive. Silver is rebuildable from bronze.
- REST snapshots are ground truth anchors. WS snapshots are per-subscription conveniences.
- `max(0, old + delta)` — clamp to zero, remove level if zero.

## Environment

- Python 3.12, shares repo-level `.venv/`
- AWS credentials via `~/.aws/credentials`
- `.env` requires `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`
- S3 bucket: `prediction-markets-data`, prefix: `mm/`

## Commands

```bash
source .venv/bin/activate
python -m pytest market-maker/tests/ -v
python -m market-maker.scripts.collect                        # collect crypto (default: KXBTC, KXETH, KXSOL)
python -m market-maker.scripts.collect --prod                 # production API
python -m market-maker.scripts.collect --series KXBTC KXETH   # specific series
python -m market-maker.scripts.collect --series               # ALL markets (not recommended)
python -m market-maker.scripts.rebuild                        # batch reconstruct silver from bronze
```
