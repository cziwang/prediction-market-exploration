# v2/app/transforms

Stateful transforms that convert raw WebSocket frames into typed domain events.

## kalshi_ws.py

Maintains an in-memory order book per ticker (`OrderBookState`) and produces typed events from raw Kalshi WS frames:

- `orderbook_snapshot` → builds full book state, emits `OrderBookUpdate` (BBO + top-of-book size)
- `orderbook_delta` → applies incremental update to book, emits `OrderBookUpdate`
- `trade` → emits `TradeEvent` (price, size, taker side)
- Connection change (new `conn_id`) → emits `BookInvalidated` for every tracked ticker, clears all books

Key design decisions:
- **Integer cents throughout** — no floats for prices or sizes. Kalshi sends 4-decimal dollar strings (`"0.5200"`), converted via `_dollars_to_cents()` → `52`
- **Single execution site** — called synchronously in the ingester loop. Same transform runs in live and replay, so parity is structural, not disciplinary
- **MIN_SIZE filter (50 cents)** — levels below this are floating-point artifacts from accumulated deltas, not real orders

Copied from v1 — identical logic.
