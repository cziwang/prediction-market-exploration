# v2/app/core

Shared configuration, data types, and conversion utilities.

## config.py

- `S3_BUCKET` — target S3 bucket, defaults to `prediction-markets-data` (overridable via `.env`)
- `SILVER_VERSION = 3` — partition version for silver Parquet files

## conversions.py

Shared conversion functions used across the codebase:
- `dollars_to_cents(s: str) -> int` — Kalshi 4-decimal dollar string to integer cents (`"0.5200"` → `52`)
- `parse_ts(ts) -> float | None` — Kalshi timestamp (epoch int/float, ISO string, None) to float seconds

## book_state.py

`BookState` — the canonical full-depth order book for one ticker. Used by both the live ingester and the replay engine.

- Maintains `yes_book` / `no_book` as `dict[int, int]` (price_cents → resting size)
- `from_snapshot(msg)` — seeds from Kalshi `orderbook_snapshot` frame
- `apply_delta(price_cents, delta, side)` — applies a single size change
- BBO properties: `best_bid`, `best_ask`, `bid_size`, `ask_size`, `spread`, `mid`
- Full depth: `levels(side)`, `to_snapshot()`, `validate()`

`extract_depth_row(book, t_receipt_ns, t_exchange_ns, ticker, seq, sid)` — extracts a 53-column flat dict for the `OrderBookDepth` silver table (top-10 bid/ask levels + aggregate depth metrics).
