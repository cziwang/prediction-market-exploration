# v2/app/replay

Full-depth order book reconstruction from bronze data.

Unlike the BBO-only `OrderBookUpdate` in silver (which only stores best bid/ask), replay builds the complete book at every price level by replaying raw `orderbook_snapshot` + `orderbook_delta` frames from bronze.

## book_state.py

`ReplayBookState` — full-depth order book for one ticker. Maintains `yes_book` and `no_book` as `dict[int, int]` (price_cents → resting size).

- `from_snapshot(msg)` — seeds book from a Kalshi `orderbook_snapshot` frame. Handles both full snapshots (with `yes_dollars_fp`/`no_dollars_fp` price levels) and empty snapshots (settled/inactive markets with no orders)
- `apply_delta(price_cents, delta, side)` — applies a single size change at one price level. Positive delta = contracts added, negative = removed. Clears the level if size drops to zero or below
- `bbo()` — returns `(best_bid, best_ask, bid_size, ask_size)` derived from the full book. Best ask = `100 - max(no_book)` because Kalshi is a binary market (YES + NO = $1.00)
- `levels(side)` — all non-empty price levels sorted ascending
- `to_snapshot()` — full book state as a dict for serialization
- `validate()` — checks invariants: no negative sizes, no prices outside 1-99, no crossed book (bid >= ask). Returns list of `BookValidationError`

Key difference from `transforms/kalshi_ws.py`'s `OrderBookState`:
- No `MIN_SIZE` filter — preserves all levels including small ones
- No snapshot requirement — books can be built from deltas alone (critical for bronze replay where many tickers' first message is a delta, not a snapshot)
- Tracks `seq` and `sid` for gap detection

## engine.py

`ReplayEngine` — reads bronze S3 data for a date, replays all events in `(sid, seq)` order, and emits `BookSnapshot` objects.

- Loads `orderbook_snapshot` + `orderbook_delta` from `bronze/kalshi_ws/` on S3
- Sorts all records by `(sid, seq)` for correct ordering
- Snapshots seed books, deltas modify them. Tickers without a prior snapshot get an empty book created on first delta (no silent drops)
- Configurable snapshot modes: `every_event` (after every delta) or `every_n` (every N deltas per ticker)
- Optional ticker filtering — pass `tickers={"KXNBA-..."}` to only track specific markets
- Sequence gap detection when replaying all tickers (disabled when filtering, since gaps are expected from other tickers' deltas)
- Returns `(list[BookSnapshot], ReplayStats)` with processing stats
