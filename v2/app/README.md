# v2/app

Core application code for the v2 data infrastructure. No trading strategy — v2 is data-only.

## events.py

Typed domain event dataclasses (`TradeEvent`, `BookInvalidated`, `MMFillEvent`, etc.) and `TransformResult` container.

`TransformResult` is the return type of `KalshiTransform.__call__()`:
- `events: list[Event]` — TradeEvent, BookInvalidated
- `depth_rows: list[dict]` — OrderBookDepth rows (pre-formatted with ns timestamps)

`OrderBookUpdate` is deprecated — kept for reading old v=3 silver data.

### Key silver fields

| Field | Source | What it represents |
|---|---|---|
| `t_receipt_ns` | Our clock | When we saw the event. Primary sort key. |
| `t_exchange_ns` | Kalshi server | When the exchange processed it. Null on snapshots. |
| `sid` / `seq` | WS frame | Subscription ID + sequence number for gap detection. |
| `market_ticker` | WS frame | Kalshi market identifier. Dictionary-encoded. |
| `bid_1`..`bid_10` | BookState | Top 10 bid prices (YES book, descending). |
| `ask_1`..`ask_10` | BookState | Top 10 ask prices (derived from NO book, ascending). |
| `bid_depth_5c` / `ask_depth_5c` | BookState | Total contracts within 5c of BBO. |
| `side` / `price` / `size` | Trades | Taker side, execution price (cents), contract count. |

## Subdirectories

- **`core/`** — `BookState` (canonical order book), `extract_depth_row()`, `dollars_to_cents()`, `parse_ts()`, config
- **`clients/`** — Kalshi API clients for market discovery
- **`services/`** — Async S3 writers (bronze + silver)
- **`transforms/`** — `KalshiTransform`: WS frame → TransformResult (events + depth rows)
