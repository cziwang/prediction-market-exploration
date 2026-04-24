# Deploying the MM Strategy: Live Trading (Phase 2)

## Overview

This doc covers deploying the market making strategy in **live trading mode** — real
orders placed on Kalshi via REST, with fills and order state managed via WS push channels.
This is the successor to Phase 1 (paper trading, see [`deploy-mm-paper.md`](deploy-mm-paper.md)).

**Do not deploy Phase 2 until all Phase 1 graduation criteria are met.**

## Prerequisites

- Phase 1 graduation criteria met over >= 10 game-days (see [`deploy-mm-paper.md`](deploy-mm-paper.md))
- Kalshi WS ingester deployed and running (see [`live-kalshi-ws-service.md`](live-kalshi-ws-service.md))
- Kalshi account funded (Phase 2 starts with ~$50-100 max exposure)
- `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in `.env` with order placement permissions
- AWS credentials configured for S3 writes (bronze + silver)

## Architecture change from Phase 1

| Concern | Phase 1 (paper) | Phase 2 (live) |
|---|---|---|
| Order placement | `PaperOrderClient` (in-memory) | `KalshiOrderClient` → REST API |
| Order ACKs | Instant (synchronous) | `user_orders` WS push channel |
| Fill detection | Trade-stream matching | `fill` WS push channel |
| Market close | Not handled | `market_lifecycle_v2` WS push channel |
| Position tracking | Internal only | Internal + `market_positions` WS sanity check |
| Startup recovery | None (no real orders) | `bootstrap()`: cancel orphans, seed positions from REST |
| Circuit breaker | None (no REST calls) | Opens after 3 consecutive REST failures |
| Reconciliation | None | REST fallback every 30s (positions + open orders) |

## Configuration

Phase 2 starts extra conservative:

| Parameter | Phase 2 default | Phase 1 default | Reason for change |
|---|---|---|---|
| `order_size` | 1 | 1 | Same — $1 max exposure per fill |
| `max_position` | 5 | 10 | Lower until live behavior validated |
| `min_spread_cents` | 5 | 3 | Only wide spreads until fill quality confirmed |
| `min_edge_cents` | 2 | 1 | Extra fee cushion for live |
| `max_aggregate_position` | 100 | 200 | Lower until live behavior validated |
| `circuit_breaker_threshold` | 3 | N/A | Opens after 3 consecutive REST failures |
| `reconcile_interval_s` | 30 | N/A | REST fallback reconciliation frequency |

Override by modifying `MMConfig()` in `scripts/live/kalshi_ws/__main__.py::_main()`.

## Activation

### On EC2 (systemd)

Edit the existing service unit:

```bash
sudo systemctl edit kalshi-ws-ingester.service
```

Add the override:

```ini
[Service]
Environment=MM_ENABLED=1
Environment=MM_LIVE=1
```

Then restart:

```bash
sudo systemctl restart kalshi-ws-ingester.service
journalctl -u kalshi-ws-ingester.service -f
```

You should see:

```
INFO live.kalshi_ws: MM strategy enabled (LIVE mode): MMConfig(min_spread_cents=5, ...)
INFO live.kalshi_ws: subscribing to private channels: fill, user_orders, market_lifecycle_v2, market_positions
INFO strategy.mm: bootstrap: fetched 0 positions, cancelled 0 orphan orders
```

### Local (foreground, for testing)

```bash
source .venv/bin/activate
MM_ENABLED=1 MM_LIVE=1 python -m scripts.live.kalshi_ws
```

### Deactivation (emergency)

```bash
# Option 1: disable strategy, keep ingester running
sudo systemctl edit kalshi-ws-ingester.service
# Remove MM_ENABLED and MM_LIVE lines
sudo systemctl restart kalshi-ws-ingester.service

# Option 2: stop everything
sudo systemctl stop kalshi-ws-ingester.service
```

On deactivation, the shutdown handler:
1. Cancels all resting orders via REST `cancel_all()`
2. Drains the order queue
3. Flushes silver writers
4. Exits cleanly

**If the process crashes without clean shutdown**, orphaned orders may remain on Kalshi.
The next startup's `bootstrap()` will detect and cancel them.

## Startup sequence

```
1. Load config, initialize order client with Kalshi REST credentials
2. bootstrap():
   a. Fetch current positions from REST get_positions()
   b. Fetch open orders from REST get_open_orders()
   c. Cancel all orphaned orders from prior session
   d. Seed internal _positions from Kalshi's reported positions
   e. Log: "bootstrap complete: N positions, M orphan orders cancelled"
3. Connect to Kalshi WS (authenticated)
4. Subscribe to public channels: orderbook_delta, trade
5. Subscribe to private channels: fill, user_orders, market_lifecycle_v2, market_positions
6. Start order queue drainer task
7. Start reconciliation loop task
8. Begin processing frames
```

## WS channel subscriptions

```python
# Public channels — per-series, with market_tickers
for series in SERIES_TICKERS:
    tickers = fetch_open_tickers(series)
    ws.subscribe(channels=["orderbook_delta", "trade"], market_tickers=tickers)

# Private channels — global (no market_tickers filter needed)
ws.subscribe(channels=["fill"])           # our fills only
ws.subscribe(channels=["user_orders"])    # our order state changes only
ws.subscribe(channels=["market_lifecycle_v2"])  # all market lifecycle events
ws.subscribe(channels=["market_positions"])     # our position updates only
```

## Monitoring

### During a game

```bash
journalctl -u kalshi-ws-ingester.service -f | grep -E "(quote|fill|order|MISMATCH|CIRCUIT|ORPHAN|bootstrap)"
```

Expected log patterns:

```
INFO  strategy.mm: quote KXNBAPTS-... bid=40 ask=45 pos=0 spread=5
INFO  strategy.mm: order placed KXNBAPTS-... bid@40 client_order_id=abc123
INFO  strategy.mm: order ack KXNBAPTS-... order_id=ee587a1c status=resting
INFO  strategy.mm: fill KXNBAPTS-... buy@40 size=1 pos=0->1 fee=1c
INFO  strategy.mm: market close KXNBAPTS-... event_type=determined
```

### Red flags

| Symptom | Likely cause | Action |
|---|---|---|
| `CIRCUIT BREAKER OPEN` | REST API down or rate limited | Wait for auto-recovery; check Kalshi status page |
| `POSITION DRIFT` | Fill missed or double-counted | Strategy auto-corrects from `market_positions`; investigate logs |
| `ORPHAN ORDER` on startup | Prior crash left orders | Auto-cancelled by `bootstrap()`; verify in Kalshi dashboard |
| No `order ack` after placement | `user_orders` channel not delivering | Check WS connection; verify private channel subscription |
| No `fill` messages despite trades at our price | `fill` channel issue | Check WS; compare against public trade stream |
| `on_order_rejected` errors | Market closed, insufficient funds, etc. | Check error message; may need to fund account |
| Orders accumulating in queue | REST latency or rate limiting | Check queue depth; may need to increase drain rate |

### Post-game analysis

Same as Phase 1, but now with authoritative fill data:

```python
# Load fills with real fees and positions
fills = load_silver("MMFillEvent", date="2026-04-25")

# Live fills include authoritative fee_cost from Kalshi
# (not the estimated maker_fee_cents from paper mode)
print(f"Total fees paid: ${fills['maker_fee'].sum() / 100:.2f}")

# Position reconciliation events — any mismatches detected?
reconcile = load_silver("MMReconcileEvent", date="2026-04-25")
if len(reconcile) > 0:
    print(f"WARNING: {len(reconcile)} position/order mismatches detected")
```

### Metrics to track

Same as Phase 1, plus:

| Metric | What it tells you | Target |
|---|---|---|
| REST round-trip latency | Order placement speed | < 500ms p99 |
| WS ACK latency | Time from REST call to `user_orders` push | < 200ms p99 |
| Position drift events | `market_positions` vs internal mismatch | Zero |
| Circuit breaker trips | REST reliability | Zero (investigate each one) |
| Orphan orders on startup | Clean shutdown reliability | Zero after first week |
| Fill channel vs trade stream | Are we seeing all our fills? | 100% match |

## Reconciliation

The reconciliation loop runs every 30s as a safety net behind the WS push channels:

1. Fetch positions from REST `get_positions()`
2. Compare against internal `_positions` (which is updated by `fill` and `market_positions` channels)
3. If mismatch: log `POSITION MISMATCH`, trust Kalshi, emit `MMReconcileEvent`
4. Fetch open orders from REST `get_open_orders()`
5. Compare against internal order state
6. If orphan found: cancel it, log `ORPHAN ORDER`, emit `MMReconcileEvent`

In steady state, this loop should find zero mismatches — the WS channels are the
primary source of truth. Mismatches indicate a bug or a WS delivery failure.

## Rollback to paper mode

```bash
sudo systemctl edit kalshi-ws-ingester.service
# Change MM_LIVE=1 to MM_LIVE=0 (or remove it)
sudo systemctl restart kalshi-ws-ingester.service
```

The shutdown handler cancels all resting orders before exiting. On restart with
`MM_LIVE=0`, the strategy reverts to `PaperOrderClient` — no real orders, same
as Phase 1.

## Risk limits

### Hard limits (enforced in code, not configurable at runtime)

| Limit | Value | Enforcement |
|---|---|---|
| Max single order size | 10 contracts | `KalshiOrderClient.place_limit()` rejects |
| Max per-ticker position | configurable, default 5 | Strategy suppresses order side |
| Max aggregate position | configurable, default 100 | Strategy suppresses all orders |
| Circuit breaker | 3 consecutive REST failures | Suppresses all new order intents |

### Soft limits (monitored, not enforced)

| Limit | Alert threshold | Action |
|---|---|---|
| Daily loss | -$10 | Investigate; consider kill switch |
| Open positions at game end | > 5 tickers | Investigate inventory management |
| Quote-to-trade ratio | > 50 | May hit REST rate limits |

## Graduating to Phase 3

### Criteria (over >= 10 game-days of live trading)

| Criterion | Threshold |
|---|---|
| Realized P&L (round-trips) | Positive |
| Live fill prices vs paper expectations | Within 1c median |
| Position drift events | Zero |
| Circuit breaker trips | Zero |
| REST errors | < 1% of order intents |
| WS channel reliability | No missed fills (validated by reconciliation) |

### Phase 3 changes

- Lower `min_spread_cents` to 3 (the simulation breakeven)
- Increase `order_size` to 5-10 contracts
- Increase `max_position` to 20
- Add pre-settlement flattening (market orders to close positions before `determined`)
- Add inventory skewing (already in strategy, just lower `skew_threshold`)
