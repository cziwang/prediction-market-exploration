# Backtest Readiness — Data infrastructure for strategy development

## Status: Refactoring pipeline — unifying book state and depth table

---

## Background: How bronze data works

Bronze is raw WebSocket frames saved as gzip-JSONL. Every message Kalshi sends gets written to `bronze/kalshi_ws/{channel}/{Y}/{M}/{D}/{H}/{uuid}.jsonl.gz` with a wrapper:

```json
{
  "source": "kalshi_ws",
  "channel": "orderbook_delta",
  "t_receipt": 1777163362.84,
  "conn_id": "278dfd18c1644f69b2b470f47d49ad36",
  "frame": {
    "type": "orderbook_delta",
    "sid": 1,
    "seq": 1207226,
    "msg": {
      "market_ticker": "KXNBAPTS-26APR25NYKATL-ATLJJOHNSON1-15",
      "price_dollars": "0.3800",
      "delta_fp": "-300.00",
      "side": "yes",
      "ts": "2026-04-26T00:29:22.803263Z",
      "ts_ms": 1777163362803
    }
  }
}
```

- `t_receipt` — when OUR EC2 server received the WS message. Unix float seconds.
- `conn_id` — UUID identifying which WS connection produced this frame. Changes on reconnect.
- `frame.sid` — subscription ID. Messages with the same sid share a sequence space.
- `frame.seq` — sequence number, monotonically increasing per sid. Gaps = missed messages.
- `frame.msg` — the Kalshi payload (varies by channel).

### Three channels

1. **`orderbook_snapshot`** — Full book state for one ticker. `yes_dollars_fp` / `no_dollars_fp` = `[price_str, size_str]` tuples. Best bid = `max(yes)`, best ask = `100 - max(no)` (binary market). Empty snapshots (no levels) = settled/inactive markets.

2. **`orderbook_delta`** — Single price level change. `price_dollars`, `delta_fp` (positive=add, negative=remove), `side` (yes/no), `ts`/`ts_ms` (exchange timestamp).

3. **`trade`** — Executed trade. `yes_price_dollars`, `no_price_dollars` (sum to $1), `count_fp` (contracts), `taker_side`.

### Book reconstruction

Start with snapshot → apply deltas in order → after each delta, book reflects current state. Trades also cause deltas (filled order's size decreases).

---

## Current data assets

| Layer | Table | Rows/day | Status |
|-------|-------|----------|--------|
| Bronze | Raw WS frames (gzip-JSONL) | all | Running on EC2 |
| Silver | OrderBookDepth (top-10 levels + metrics, 53 cols) | ~5M | Built for 4/26, needs backfill |
| Silver | OrderBookUpdate (BBO only, 9 cols) | ~5M | Exists but being deprecated |
| Silver | TradeEvent | ~600K | Running |
| Silver | BookInvalidated | ~50 | Running |
| Silver | MM* events (quotes, orders, fills) | ~500 | Running |
| Reference | market_metadata (117K markets, settlements) | — | Done |

### OrderBookDepth schema (53 columns)

The primary book data table. One row per book-changing delta.

```
t_receipt_ns, t_exchange_ns, market_ticker, seq, sid
bid_1..bid_10 (prices), bid_1_size..bid_10_size
ask_1..ask_10 (prices), ask_1_size..ask_10_size
bid_depth_5c, ask_depth_5c, bid_depth_10c, ask_depth_10c
num_bid_levels, num_ask_levels, spread, mid_x2
```

- Bid levels: sorted descending (bid_1 = best = highest YES price)
- Ask levels: sorted ascending (ask_1 = best = cheapest, derived as `100 - max(no_book)`)
- Null when fewer than N levels exist
- Preserves all levels including tiny orders (no MIN_SIZE filter). ~93% of rows show negative spread due to transient 1-contract orders at crossing prices. Apply size filters in queries.
- `mid_x2 = bid_1 + ask_1` (doubled to avoid half-cent floats)

### Crossed books note

Raw depth data shows ~93% transient crossed books because it preserves all levels. The old BBO silver applied `MIN_SIZE=50` (ignored levels < 50 contracts), producing 0% crosses. For research, apply your own size filter: `WHERE bid_1_size >= 50 AND ask_1_size >= 50`.

---

## Pipeline refactor — completed 2026-05-02

### What was done

1. **Extracted shared utilities** → `app/core/conversions.py` (`dollars_to_cents`, `parse_ts`)
2. **Unified BookState** → `app/core/book_state.py` (single class, deleted `OrderBookState` and `ReplayBookState`)
3. **Live ingester produces OrderBookDepth** → `KalshiTransform` returns `TransformResult(events, depth_rows)`, `SilverWriter.emit_row()` handles pre-formatted dicts, `OrderBookUpdate` deprecated
4. **Consolidated scripts** → `build_silver.py` replaces old backfill + depth scripts. Deleted `app/replay/` module (engine, book_state shim).
5. **Tests** → 40 tests (30 BookState + extract_depth_row, 10 silver writer)

### Architecture

```
LIVE INGESTER (continuous, EC2):
  bronze
  + OrderBookDepth (real-time, 53 columns)
  + TradeEvent
  + BookInvalidated

DAILY CRON (EC2, 07:00 UTC):
  compact_silver      — merge small files
  fetch_markets       — refresh settlement outcomes

BACKFILL (one-time, per historical date):
  build_silver --date YYYY-MM-DD    — rebuild all silver from bronze
```

### Key files

| File | Role |
|------|------|
| `app/core/book_state.py` | `BookState` + `extract_depth_row()` |
| `app/core/conversions.py` | `dollars_to_cents()` + `parse_ts()` |
| `app/transforms/kalshi_ws.py` | `KalshiTransform` → `TransformResult` |
| `app/services/silver_writer.py` | `emit()` for events, `emit_row()` for depth dicts |
| `scripts/build_silver.py` | Unified batch builder |
| `scripts/live/kalshi_ws/` | Live ingester |

### Next steps

- Deploy updated live ingester to EC2 (test locally first — it now produces OrderBookDepth instead of OrderBookUpdate)
- Run `build_silver --start 2026-04-22 --end 2026-05-01` to backfill historical dates
- Start strategy research using OrderBookDepth + TradeEvent + market_metadata

---

## Data coverage

- ~10 days bronze (4/22 — 5/2), accumulating daily through NBA playoffs (mid-June)
- 117,900 markets in metadata (113,613 with settlement results)
- OrderBookDepth built for 4/26 (verified), needs backfill for remaining dates
- Live ingester running on EC2 (currently producing BBO — needs redeployment for depth)

## Athena tables

Database: `prediction_markets`, workgroup: `prediction-markets` (10 GB scan cutoff)

| Table | Type | Partitions |
|-------|------|------------|
| `order_book_depth` | Silver | date + v=3 |
| `order_book_update` | Silver (deprecated) | date + v=3 |
| `trade_event` | Silver | date + v=3 |
| `book_invalidated` | Silver | date + v=3 |
| `mm_quote_event` | Silver | date + v=3 |
| `mm_order_event` | Silver | date + v=3 |
| `mm_fill_event` | Silver | date + v=3 |
| `mm_reconcile_event` | Silver | date + v=3 |
| `mm_circuit_breaker_event` | Silver | date + v=3 |
| `market_metadata` | Reference | none |
