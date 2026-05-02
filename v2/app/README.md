# v2/app

Core application code for the v2 data infrastructure. No trading strategy — v2 is data-only.

## events.py

Typed domain event dataclasses (`OrderBookUpdate`, `TradeEvent`, `MMFillEvent`, etc.). These are the contract between the live pipeline and the storage layer. Every downstream component consumes these objects.

Extends v1 with fields previously dropped from raw WS frames: `t_exchange`, `sid`, `seq`. `t_receipt` and `t_exchange` stay as `float` (seconds) internally; conversion to `int64` nanoseconds happens at the silver writer serialization boundary.

### Silver event fields

What each field in the v=3 silver schema is, where it comes from, and why it matters.

#### Included fields

| Field | Source | What it represents | Why it matters |
|---|---|---|---|
| `t_receipt_ns` | Our clock (`time.time()` on frame receive) | When *we* saw the event | Primary sort key. Always present, monotonically increasing. Used for all time-range queries. |
| `t_exchange_ns` | `frame.msg.ts` (Kalshi server) | When Kalshi's matching engine processed the event | `t_receipt_ns - t_exchange_ns` = **gateway latency**. Use for stale book detection (widen spreads if latency spikes), latency monitoring (track p50/p99), and fill analysis (was the book mid at *exchange* time different from when we heard about it?). Null on snapshots. |
| `sid` | `frame.sid` | Subscription ID — which series subscription this message belongs to | Sequence numbers are scoped per `sid`. Without it, you can't tell if a seq jump is a real gap or just interleaving between subscriptions (e.g., `sid=1 seq=100` and `sid=2 seq=50` are independent). |
| `seq` | `frame.seq` | Position of this message within its subscription's stream | **Gap detection** — if you see seq 41 then 43, you missed message 42. A missed delta means your book is wrong, a missed trade means your volume is wrong. Detectable with a simple SQL window function: `seq - LAG(seq) OVER (PARTITION BY sid ORDER BY seq)`. |
| `market_ticker` | `frame.msg.market_ticker` | Kalshi market identifier | Dictionary-encoded (int16 index → string). ~500 unique tickers, 300k+ rows/day — 93% storage savings vs raw strings. |
| `bid_yes` / `ask_yes` | Computed from order book state | Best bid/ask price in integer cents (0–100) | Core order book data. Integer cents — no floats for money. |
| `bid_size` / `ask_size` | Computed from order book state | Top-of-book size in cents of contract value | Liquidity at BBO. |
| `side` | `frame.msg.taker_side` (trades) | "yes" or "no" — which side the taker was on | Dictionary-encoded (2 unique values). |
| `price` / `size` | `frame.msg.yes_price_dollars` / `frame.msg.count_fp` | Trade price (cents) and size | Integer cents. |

#### Excluded fields

| Field | Source | Why excluded |
|---|---|---|
| `no_price_dollars` | `frame.msg` on trades | Always `100 - price`. Pure redundancy — derive it if needed. |
| `conn_id` | Wrapper (assigned by our ingester) | Connection changes already captured via `BookInvalidated` events. High-cardinality string with minimal query value. Available in bronze for debugging. |

## Subdirectories

- `clients/` — Kalshi API clients for discovering open markets
- `core/` — Configuration (S3 bucket, silver version)
- `replay/` — Full-depth order book reconstruction from bronze data
- `services/` — Async S3 writers (bronze + silver)
- `transforms/` — Stateful raw-frame-to-typed-event conversion

### replay/

Reconstructs full-depth order books from bronze `orderbook_snapshot` + `orderbook_delta` data. Unlike the BBO-only `OrderBookUpdate` in silver (which only stores best bid/ask), replay builds the complete book at every price level.

- **`book_state.py`** — `ReplayBookState`: full-depth book for one ticker. Maintains `yes_book` and `no_book` as `dict[int, int]` (price_cents → size). Supports delta application, snapshot seeding, BBO derivation, full-depth level queries, and invariant validation (negative sizes, crossed books, price range).

- **`engine.py`** — `ReplayEngine`: reads bronze S3 data for a date, replays snapshots + deltas in sequence order, and emits `BookSnapshot` objects at configurable intervals (`every_event`, `every_n`). Handles tickers that appear without a prior snapshot (creates empty book from first delta). Detects sequence gaps when replaying all tickers.
