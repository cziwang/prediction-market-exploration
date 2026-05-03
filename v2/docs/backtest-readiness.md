# Backtest Readiness

## Status: Data infrastructure complete — ready for strategy research

---

## Data assets

| Layer | Table | Rows/day | Description |
|-------|-------|----------|-------------|
| Bronze | Raw WS frames (gzip-JSONL) | all | Authoritative archive, ~12 dates |
| Silver | **OrderBookDepth** (53 cols) | ~5M | Top-10 bid/ask levels + depth metrics |
| Silver | TradeEvent | ~600K | Executed trades (price, size, taker side) |
| Silver | BookInvalidated | ~50 | WS reconnect signals |
| Reference | market_metadata | 117,900 | Tickers, outcomes, settlements |

### OrderBookDepth schema

One row per book-changing delta. Primary table for research.

```
t_receipt_ns, t_exchange_ns, market_ticker, seq, sid
bid_1..bid_10 (prices), bid_1_size..bid_10_size
ask_1..ask_10 (prices), ask_1_size..ask_10_size
bid_depth_5c, ask_depth_5c, bid_depth_10c, ask_depth_10c
num_bid_levels, num_ask_levels, spread, mid_x2
```

- Bid levels sorted descending (bid_1 = best). Ask levels sorted ascending (ask_1 = best).
- Preserves all levels — no MIN_SIZE filter. Apply `WHERE bid_1_size >= 50` for filtered BBO.
- `mid_x2 = bid_1 + ask_1` (doubled to avoid half-cent floats)

### Market metadata

117,900 NBA markets across 13 series. 113,613 with settlement results (`yes`/`no`). Joinable on `market_ticker = ticker`.

Key fields: `ticker, series_ticker, event_ticker, title, yes_sub_title, status, result, open_time, close_time, settlement_time, volume, last_price_cents, settlement_value_cents`

---

## Architecture

```
LIVE INGESTER (continuous, EC2):
  bronze + OrderBookDepth + TradeEvent + BookInvalidated

DAILY CRON (EC2, 07:00 UTC):
  compact_silver

MANUAL (as needed):
  build_silver --all --delete-existing    rebuild silver from bronze
  fetch_markets                           refresh market metadata
```

### Key files

| File | Role |
|------|------|
| `app/core/book_state.py` | `BookState` + `extract_depth_row()` |
| `app/core/conversions.py` | `dollars_to_cents()` + `parse_ts()` |
| `app/transforms/kalshi_ws.py` | `KalshiTransform` → `TransformResult` |
| `app/services/silver_writer.py` | `emit()` for events, `emit_row()` for depth dicts |
| `scripts/build_silver.py` | Batch builder (backfill from bronze) |
| `scripts/live/kalshi_ws/` | Live ingester |

---

## Next steps

1. **Deploy updated ingester to EC2** — now produces OrderBookDepth instead of BBO
2. **Backfill historical silver** — `build_silver --all --delete-existing` (running)
3. **Strategy research** — use OrderBookDepth + TradeEvent + market_metadata

---

## Athena

Database: `prediction_markets`, workgroup: `prediction-markets` (10 GB scan cutoff)

| Table | Type |
|-------|------|
| `order_book_depth` | Silver (primary book data) |
| `trade_event` | Silver |
| `book_invalidated` | Silver |
| `market_metadata` | Reference |

BBO view if needed:
```sql
SELECT t_receipt_ns, market_ticker,
       bid_1 as bid_yes, ask_1 as ask_yes,
       bid_1_size as bid_size, ask_1_size as ask_size
FROM order_book_depth
WHERE bid_1_size >= 50 AND ask_1_size >= 50
```

---

## Bronze reference

Bronze frames: `bronze/kalshi_ws/{channel}/{Y}/{M}/{D}/{H}/{uuid}.jsonl.gz`

Three channels:
- **`orderbook_snapshot`** — full book state (`yes_dollars_fp` / `no_dollars_fp` level arrays)
- **`orderbook_delta`** — single level change (`price_dollars`, `delta_fp`, `side`, `ts`)
- **`trade`** — executed trade (`yes_price_dollars`, `count_fp`, `taker_side`, `ts`)

Book reconstruction: snapshot seeds the book, deltas modify one level at a time, `best_bid = max(yes_book)`, `best_ask = 100 - max(no_book)`.
