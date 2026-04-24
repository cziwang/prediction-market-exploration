# Deploying the MM Strategy: Paper Trading (Phase 1)

## Overview

This doc covers deploying the market making strategy in **paper trading mode** — the
strategy runs live against real Kalshi WS data, makes quoting decisions, and simulates
fills, but never places real orders. All decisions and simulated fills are logged to
silver Parquet for post-hoc analysis.

The strategy runs inside the existing Kalshi WS ingester process (`scripts/live/kalshi_ws`).
No new process, no new infra. Activation is a single environment variable: `MM_ENABLED=1`.

## Prerequisites

- Kalshi WS ingester already deployed and running (see [`docs/live-kalshi-ws-service.md`](live-kalshi-ws-service.md))
- AWS credentials configured for S3 writes (bronze + silver)
- `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in `.env`
- Python 3.12+ with project dependencies installed (`pip install -r requirements.txt`)

## Configuration

All MM configuration is in `app/strategy/mm.py::MMConfig` with sensible defaults:

| Parameter | Default | Description |
|---|---|---|
| `min_spread_cents` | 3 | Don't quote if raw spread < 3c |
| `min_edge_cents` | 1 | Don't quote if net edge (half_spread - fee) < 1c |
| `max_position` | 10 | Per-ticker position limit (contracts) |
| `max_aggregate_position` | 200 | Across all tickers |
| `skew_threshold` | 3 | Start widening one side at this position |
| `order_size` | 1 | Contracts per side per quote |
| `series_filter` | `KXNBAPTS-` | Only quote tickers matching this prefix |

For Phase 1, these defaults are conservative. Override by modifying `MMConfig()` in
`scripts/live/kalshi_ws/__main__.py::_main()`.

## Activation

### On EC2 (systemd)

Edit the existing service unit to add `MM_ENABLED=1`:

```bash
sudo systemctl edit kalshi-ws-ingester.service
```

Add a complete override block:

```ini
[Service]
Environment=MM_ENABLED=1
```

Then restart:

```bash
sudo systemctl restart kalshi-ws-ingester.service
journalctl -u kalshi-ws-ingester.service -f
```

You should see:

```
INFO live.kalshi_ws: MM strategy enabled (paper mode): MMConfig(min_spread_cents=3, ...)
```

### Local (foreground)

```bash
source .venv/bin/activate
MM_ENABLED=1 python -m scripts.live.kalshi_ws
```

### Deactivation

Remove `MM_ENABLED=1` (or set `MM_ENABLED=0`) and restart. The ingester continues
bronze archival as before — the strategy is fully opt-in.

## What happens when enabled

### Data flow

```
Kalshi WS → _archive() → bronze (unchanged)
                ↓
          KalshiTransform → OrderBookUpdate / TradeEvent / BookInvalidated
                ↓                      ↓
          SilverWriter            MMStrategy.on_event()
          (typed events)               ↓
                ↓               PaperOrderClient
          silver/kalshi_ws/     (simulated orders)
          OrderBookUpdate/           ↓
          TradeEvent/          MMQuoteEvent / MMFillEvent / MMOrderEvent
                                     ↓
                               SilverWriter
                               (strategy events)
                                     ↓
                               silver/kalshi_ws/
                               MMQuoteEvent/
                               MMFillEvent/
```

### S3 output (new prefixes)

In addition to existing bronze and typed-event silver, the strategy writes:

```
silver/kalshi_ws/MMQuoteEvent/date=YYYY-MM-DD/v=1/part-*.parquet
silver/kalshi_ws/MMFillEvent/date=YYYY-MM-DD/v=1/part-*.parquet
silver/kalshi_ws/MMOrderEvent/date=YYYY-MM-DD/v=1/part-*.parquet
```

These flush every 5 minutes (SilverWriter default) or on shutdown.

### Log output

The ingester logs strategy activity at INFO level:

```
INFO strategy.mm: quote KXNBAPTS-26APR20ATLNYK-TREYOU25 bid=40 ask=45 pos=0 spread=5
INFO strategy.mm: fill KXNBAPTS-26APR20ATLNYK-TREYOU25 buy@40 pos=0→1
```

Errors in the transform or strategy are caught per-event and logged at ERROR level
without crashing the ingester:

```
ERROR live.kalshi_ws: strategy error on OrderBookUpdate
Traceback ...
```

## Monitoring

### During a game

Watch the logs for quoting and fill activity:

```bash
journalctl -u kalshi-ws-ingester.service -f | grep -E "(quote|fill|MM)"
```

Expected behavior:
- Quotes placed on KXNBAPTS tickers with spread >= 3c
- Fills when trades match our posted prices
- Position updates after each fill
- No quotes on KXNBAGAME/KXNBASPREAD/KXNBATOTAL (filtered out)

### Red flags

| Symptom | Likely cause | Action |
|---|---|---|
| No quotes at all | No KXNBAPTS markets open, or all spreads < 3c | Wait for game time; check `min_spread_cents` |
| Quote storms (>100/s) | Bug in change detection | Set `MM_ENABLED=0`, restart, investigate |
| Position growing unbounded | Position limit not working | Check `max_position` config; review fills |
| `strategy error` in logs | Bug in `on_event()` code | Non-fatal (ingester continues); fix the bug |
| `transform error` in logs | Unexpected frame format from Kalshi | Check frame in bronze; update transform |
| No recent silver files | SilverWriter flush failing silently | Check: `aws s3 ls s3://prediction-markets-data/silver/kalshi_ws/MMQuoteEvent/date=$(date +%Y-%m-%d)/` |
| Memory growing steadily | Stale tickers accumulating in dicts | Restart; addressed in known limitations below |

### Post-game analysis

Check that silver data landed:

```bash
aws s3 ls s3://prediction-markets-data/silver/kalshi_ws/MMFillEvent/date=$(date +%Y-%m-%d)/ --recursive
```

Load and analyze in a notebook:

```python
import pandas as pd, io, boto3

s3 = boto3.client("s3")

# List all fill Parquet parts for today
prefix = "silver/kalshi_ws/MMFillEvent/date=2026-04-23/v=1/"
keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(
    Bucket="prediction-markets-data", Prefix=prefix) for o in page.get("Contents", [])]

fills = pd.concat([pd.read_parquet(io.BytesIO(
    s3.get_object(Bucket="prediction-markets-data", Key=k)["Body"].read()))
    for k in keys], ignore_index=True)

# Round-trip P&L: match buy/sell fills per ticker
buys = fills[fills["side"] == "buy"].sort_values("t_receipt")
sells = fills[fills["side"] == "sell"].sort_values("t_receipt")
print(f"Fills: {len(fills)} ({len(buys)} buys, {len(sells)} sells)")

# Per-fill P&L (sell_price - buy_price - 2*fee for matched pairs)
# See notebooks/strategies/market_making.ipynb for the full analysis framework
```

### Metrics to track each game night

| Metric | What it tells you | Target |
|---|---|---|
| Fill rate | % of posted quotes that result in fills | Track trend, no fixed target |
| Quote survival time | How long quotes rest before fill/cancel | > 1s median |
| Adverse selection (5s) | Mid-price move against us after fill | < half-spread net of fees |
| Inventory half-life | How long until position mean-reverts | < 30 minutes |
| Quote-to-trade ratio | Quotes placed per fill received | < 50 (REST rate limit feasibility) |
| Ticker concentration | % of P&L from top ticker | No single ticker > 30% |
| Open inventory at game end | Unsettled contracts | Track for settlement risk |

See `notebooks/strategies/market_making.ipynb` for the full analysis framework.

## Testing before deploy

Run the test suite to verify the implementation:

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

All 32 tests should pass. The tests cover:
- Transform: snapshot/delta/trade parsing, conn_id invalidation
- Strategy: quoting, state machine, position limits, skewing, fills
- Integration: full pipeline with fixture frames

## Rollback

1. Set `MM_ENABLED=0` and restart the service
2. The strategy stops immediately -- no cleanup needed (paper mode has no real orders)
3. Bronze archival continues unaffected
4. Silver strategy events from the session remain on S3 for analysis

**Note on restart:** all in-memory state (positions, order states, book state) is lost
on restart. Post-restart P&L analysis must partition by session. Silver events carry
`t_receipt` timestamps that can be used to identify session boundaries (gap in
timestamps = restart).

## Known limitations of paper trading

### Biases that overstate performance

Paper trading systematically overstates P&L compared to live trading. Be aware of
these biases when interpreting results:

1. **No queue priority.** `PaperOrderClient` fills our order whenever *any* trade
   occurs at our price, regardless of how much size was ahead of us in the queue.
   Real fills would require the trade to exhaust all prior queue depth first.
   **Impact:** fill rate is overstated, possibly by 2-10x on liquid tickers.

2. **Instant ACKs.** Order placement and cancellation are synchronous in paper mode.
   Live mode has async REST round-trips (~100-200ms) during which the strategy is
   in `pending`/`cancel_pending` state and cannot react. Paper mode overstates
   time-in-market.

3. **No cancel-to-repost latency.** When a price changes, paper mode cancels and
   re-places on the next book update. Live mode has a cancel-ack round-trip during
   which the strategy is dark on that side.

4. **`book_mid_at_fill=0`.** Adverse selection measurement is not populated in paper
   mode. The strategy should be updated to store the last-seen mid per ticker and
   populate this field (TODO for a follow-up).

5. **Settlement not modeled.** Paper positions are never settled. A strategy that
   accumulates directional inventory and gets lucky on game outcomes looks profitable;
   one that gets unlucky looks terrible. Always report open inventory at game end
   separately from spread P&L.

### Memory growth on long runs

Several in-memory structures grow without bound:
- `PaperOrderClient.order_log`: append-only list, never trimmed
- `KalshiTransform._books`: one entry per ticker, cleared only on reconnect
- `MMStrategy._order_state` / `_positions` / `_last_quote`: never evict closed tickers

For multi-day runs, restart daily (e.g., add `RuntimeMaxSec=20h` to the systemd unit)
to prevent memory growth. This is acceptable for paper trading; Phase 2 will add
proper ticker eviction.

## What this does NOT do

- **No real orders.** PaperOrderClient simulates everything locally.
- **No position reconciliation.** Internal state is the only source of truth (no Kalshi API calls).
- **No pre-settlement flattening.** Paper positions are not real -- no need to flatten.
- **No circuit breaker.** No REST API calls to break.

These are all Phase 2 concerns. See [`docs/strategy-kalshi-mm.md`](strategy-kalshi-mm.md)
for the full Phase 2+ design.

## Graduating to Phase 2 (live trading)

### Concrete criteria

Do not proceed to Phase 2 until ALL of the following are met over at least 10 game-days:

| Criterion | Threshold |
|---|---|
| Realized P&L (round-trips only, excluding open inventory) | Positive |
| Median adverse selection at 5s | < half-spread net of fees |
| Ticker concentration | No single ticker > 30% of total P&L |
| Quote-to-trade ratio | < 50 (feasible for REST API rate limits) |
| Position limit violations | Zero (state machine correctness) |
| Strategy errors in logs | Zero (no uncaught exceptions) |

### What Phase 2 requires

1. Implementing `app/clients/kalshi_rest_orders.py` (real order placement)
2. Adding the reconciliation loop and circuit breaker
3. Adding startup recovery (`bootstrap()`)
4. Replacing `PaperOrderClient` with `KalshiOrderClient` in `_main()`
5. A separate deployment doc for live trading with real money

**Do not skip Phase 1.** The paper P&L, fill rate, quote frequency, adverse selection,
and position behavior are the validation gate. Paper results that don't clear the
criteria above are a strong signal that live trading would lose money.
