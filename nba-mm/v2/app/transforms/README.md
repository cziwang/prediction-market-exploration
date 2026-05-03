# v2/app/transforms

Stateful transforms that convert raw WebSocket frames into typed events and depth rows.

## kalshi_ws.py

`KalshiTransform` — maintains a `BookState` per ticker and produces a `TransformResult` from each raw Kalshi WS frame:

- `orderbook_snapshot` → seeds full book state, emits `OrderBookDepth` row (53 columns)
- `orderbook_delta` → applies incremental update, emits `OrderBookDepth` row
- `trade` → emits `TradeEvent` (price, size, taker side)
- Connection change (new `conn_id`) → emits `BookInvalidated` for every tracked ticker, clears all books

Returns `TransformResult(events: list[Event], depth_rows: list[dict])`. Events are `TradeEvent` and `BookInvalidated` dataclasses. Depth rows are pre-formatted dicts with nanosecond timestamps, ready for `SilverWriter.emit_row()`.

Uses `BookState` from `v2.app.core.book_state` — no MIN_SIZE filtering, preserves all levels.
