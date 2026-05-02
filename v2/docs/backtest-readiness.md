# Backtest Readiness — Pre-work before strategy development

## Status: In Progress

Everything needed before we can start investigating and backtesting strategies on Kalshi NBA data.

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
      "market_id": "946d79c1-3793-4596-b67d-5ddd47643b2c",
      "price_dollars": "0.3800",
      "delta_fp": "-300.00",
      "side": "yes",
      "ts": "2026-04-26T00:29:22.803263Z",
      "ts_ms": 1777163362803
    }
  }
}
```

- `t_receipt` — when OUR EC2 server received the WebSocket message. Unix timestamp in float seconds. This is our clock, not Kalshi's.
- `conn_id` — UUID identifying which WebSocket connection produced this frame. Changes on reconnect. Useful for debugging — if you see a new conn_id mid-day, the connection dropped and reconnected.
- `frame.type` — echoes the channel name (`orderbook_delta`, `orderbook_snapshot`, `trade`, etc.)
- `frame.sid` — subscription ID. When we subscribe to Kalshi's WS, each subscription gets an integer ID. For example, `sid=1` might be the orderbook subscription, `sid=4` the trade subscription. Messages with the same sid share a sequence space.
- `frame.seq` — sequence number, monotonically increasing per sid. If you see seq go from 1207226 to 1207228 (skipping 1207227), that's a gap — a message was lost. Critical for data quality.
- `frame.msg` — the actual Kalshi payload. Structure varies by channel type (see below).

### The three channels that matter for book reconstruction

**1. `orderbook_snapshot`** — Full book state for one ticker.

Kalshi sends these when we subscribe to a ticker's order book. The snapshot gives us the complete starting state — every price level with its resting size.

```json
{
  "type": "orderbook_snapshot",
  "sid": 1,
  "seq": 1,
  "msg": {
    "market_ticker": "KXNBAGAME-26APR25DENMIN-DEN",
    "market_id": "e21f3f0b-c567-44d6-a365-c0d3208877df",
    "yes_dollars_fp": [
      ["0.0100", "2955472.00"],
      ["0.0200", "36514.00"],
      ["0.0500", "239108.00"],
      ["0.1100", "264268.00"],
      ["0.5500", "9863.00"]
    ],
    "no_dollars_fp": [
      ["0.0100", "1234.00"],
      ["0.0200", "5678.00"],
      ["0.4400", "40844.00"]
    ]
  }
}
```

**Understanding `yes_dollars_fp` and `no_dollars_fp`:**

Each is a list of `[price, size]` tuples representing resting limit orders:

- `yes_dollars_fp` — limit orders to BUY YES contracts (i.e., bids on the YES side). Each tuple: `["0.5500", "9863.00"]` means there are 9,863 contracts resting at 55 cents on the YES book. The highest price in this list is the **best bid** (the most someone is willing to pay for YES).

- `no_dollars_fp` — limit orders to BUY NO contracts. Each tuple: `["0.4400", "40844.00"]` means 40,844 contracts resting at 44 cents on the NO book.

**How YES and NO relate (Kalshi is a binary market):**
- Buying YES at 55c is equivalent to selling NO at 45c (they sum to $1.00)
- The **best ask** (cheapest price to buy YES) = `100 - max(no_dollars_fp prices)`. If the highest NO bid is 44c, then the cheapest YES ask is 56c.
- So from the example: best bid = 55c (from yes book), best ask = 56c (from 100 - 44c no book), spread = 1c.

Prices are dollar strings (Kalshi's format). `"0.5500"` = 55 cents. We convert to integer cents at the transform boundary.

Sizes are floating-point strings (the `_fp` suffix). `"9863.00"` = 9,863 contracts. We convert to integers via `int(round(float(...)))`.

**Two flavors of snapshots in bronze:**

- **With price levels (1,162 on 4/26):** Contains full `yes_dollars_fp` and `no_dollars_fp` arrays as shown above. Sent when we subscribe to an active market with resting orders.

- **Without price levels (454 on 4/26):** Just `market_ticker` and `market_id`, no price arrays. This happens when the book is genuinely empty — there are zero resting orders. The most common reasons:
  - **Settled markets:** The game already happened. `KXNBAGAME-26APR25NYKATL-NYK` is an April 25 game — by April 26 it's settled, all orders are cleared. 59 of the 129 empty-only tickers on 4/26 were APR25 games.
  - **Not-yet-active markets:** Future games that haven't attracted orders yet. 26 empty-only tickers were APR28 games.
  - **Game-day markets pre/post-game:** 37 were APR26 tickers, likely captured before the market opened or after it settled.
  
  These aren't broken — they're accurate snapshots of an empty book. The replay engine just needs to handle them correctly (initialize an empty book, let subsequent deltas build it up if orders arrive).

**2. `orderbook_delta`** — A single change to one price level on one side of one ticker's book.

This is the bulk of the data (~140 MB/day). Every time someone places, cancels, or gets filled on an order, Kalshi sends a delta.

```json
{
  "type": "orderbook_delta",
  "sid": 1,
  "seq": 1207226,
  "msg": {
    "market_ticker": "KXNBAGAME-26APR25DENMIN-MIN",
    "price_dollars": "0.2500",
    "delta_fp": "-200.00",
    "side": "yes",
    "ts": "2026-04-26T00:29:22.803263Z",
    "ts_ms": 1777163362803
  }
}
```

Field breakdown:
- `price_dollars` — which price level changed. `"0.2500"` = 25 cents.
- `delta_fp` — how the resting size changed. **Positive = contracts added** (new limit order placed), **negative = contracts removed** (order cancelled or filled). `"-200.00"` means 200 contracts were removed from the 25c level.
- `side` — which book: `"yes"` or `"no"`.
- `ts` / `ts_ms` — Kalshi's exchange timestamp (when the event actually happened on their matching engine). This is different from `t_receipt` (when we received the message). The difference is network latency.

**How to apply a delta to the book:**
```
current_size = book[price_cents]  # e.g., 1000 contracts at 25c
new_size = current_size + delta   # 1000 + (-200) = 800
if new_size <= 0:
    remove price level from book
else:
    book[price_cents] = new_size  # now 800 contracts at 25c
```

**Example sequence — what a cancel + re-place looks like:**
```
seq=1732993  price=0.19  delta=-10000  side=yes   (cancel: remove 10k from 19c)
seq=1732994  price=0.17  delta=+10000  side=yes   (place:  add 10k at 17c)
```
Someone moved their 10,000-contract bid from 19c down to 17c. Two deltas, one logical action.

**3. `trade`** — An executed trade (contracts changed hands).

```json
{
  "type": "trade",
  "sid": 4,
  "seq": 199060,
  "msg": {
    "trade_id": "27b6646f-086b-6540-b783-6214967d3679",
    "market_ticker": "KXNBAGAME-26APR25DENMIN-MIN",
    "yes_price_dollars": "0.3400",
    "no_price_dollars": "0.6600",
    "count_fp": "42.16",
    "taker_side": "yes",
    "ts": 1777167276,
    "ts_ms": 1777167276881
  }
}
```

Field breakdown:
- `trade_id` — unique trade identifier from Kalshi.
- `yes_price_dollars` / `no_price_dollars` — the price the trade executed at, from both perspectives. Always sums to $1.00 (`0.34 + 0.66 = 1.00`).
- `count_fp` — number of contracts traded. `"42.16"` means ~42 contracts (Kalshi uses fractional contracts internally, we round to int).
- `taker_side` — who initiated the trade. `"yes"` means a buyer of YES hit a resting NO order (or equivalently, crossed the ask). `"no"` means a buyer of NO hit a resting YES order.
- `ts` / `ts_ms` — exchange timestamp.

Note: trades also cause orderbook deltas (the filled resting order's size decreases), so you'll see a trade message AND a corresponding delta for the same event.

### How book reconstruction works

To know the full order book at any point in time:

1. **Start with a snapshot** — the `yes_dollars_fp` and `no_dollars_fp` arrays give you the complete book at that moment.
2. **Apply every subsequent delta in seq order** — each delta adds or removes contracts at one price level.
3. **After each delta, the book reflects the current state** — you can read off the full depth (all price levels and sizes), the BBO, the mid price, etc.

This is what the replay engine (Issue 2) will do.

### What the silver transform does (and why it drops data)

The `KalshiTransform` converts bronze frames into typed events. For the order book, it:

1. On `orderbook_snapshot`: creates an `OrderBookState` from the price levels, emits one `OrderBookUpdate` with just the BBO (best bid + best ask + their sizes). All other depth levels are discarded.
2. On `orderbook_delta`: looks up the existing `OrderBookState` for that ticker, applies the delta, emits an `OrderBookUpdate` with the new BBO.

**The problem in bronze replay:**

- Step 1 works for snapshots WITH price levels. But empty snapshots (settled/inactive markets) create books with empty dicts. Empty book means `best_bid` and `best_ask` are both `None`, so no BBO event is emitted. This is actually correct — there's nothing to report. But the book IS created, so subsequent deltas can modify it.
- Step 2 requires a book to already exist for that ticker (`if book is None: return events`). If the first message we see for a ticker is a delta (no prior snapshot in that day's bronze), the delta is **silently dropped**. This happens frequently because the backfill processes all bronze files for a day sorted by time, and many tickers' first appearance is a delta from a prior session's connection, not a fresh snapshot.
- Even when it works correctly, the output is just BBO (two prices, two sizes per update). All the depth information (sizes at every price level) is thrown away. This is fine for simple spread monitoring but useless for depth-aware strategies.

This is why silver `OrderBookUpdate` has 100% null `t_exchange_ns` and is missing data. The transform was designed for live WS use (where subscribe always sends full snapshots first), not for replaying archived bronze.

---

## Issue 1: Missing silver v=3 dates

**Problem:** OrderBookUpdate missing 4/22, 4/24, 4/27. TradeEvent missing some dates too. Bronze has all of them.

**Fix:** Re-run existing backfill script. The `_parse_ts` ISO string bug has been fixed (see Issue 3), so re-running will now produce `t_exchange_ns` on delta-originated BBO rows.

```bash
python -m v2.scripts.backfill_silver_v3 --delete-existing
```

**Caveat:** The backfill still uses `KalshiTransform`, which has the delta-drop bug (deltas silently skipped when no prior snapshot exists for that ticker). The BBO data will be more complete than before (timestamps work now), but still not perfect. This is acceptable — the replay engine (Issue 2) is the proper solution for full-depth book data. The BBO silver layer remains useful for quick spread/trade analysis even with its limitations.

**Status:** Not started

---

## Issue 2: Full book depth (Phase 3 — Replay Engine)

**Problem:** Silver `OrderBookUpdate` only has BBO (best bid + best ask). Backtesting market-making or depth-aware strategies requires the full order book at every price level.

**Data source:** Bronze `orderbook_delta` channel has per-level deltas:
```
price_dollars=0.25, delta_fp=-200, side=yes  →  200 contracts removed from yes book at 25c
price_dollars=0.17, delta_fp=10000, side=yes →  10000 contracts added to yes book at 17c
```

The initial `orderbook_snapshot` (with `yes_dollars_fp`/`no_dollars_fp`) provides the starting state. After that, every delta modifies one price level. Replaying snapshots + deltas in sequence order reconstructs the full book at any point in time.

Exchange timestamps (`ts`, `ts_ms`) are present on every delta — so full-depth snapshots will have exchange time, fixing Issue 3 for free.

### What to build

**`v2/app/replay/book_state.py` — ReplayBookState**
- One instance per ticker
- Maintains `yes_book: dict[int, int]` and `no_book: dict[int, int]` (price_cents → size)
- `apply_delta(price_cents, delta, side)` — adds/removes contracts at a price level
- `bbo()` → `(best_bid, best_ask, bid_size, ask_size)` — derived from the full book
- `levels(side)` → sorted list of `(price, size)` with size > 0
- `to_snapshot()` → full book state serialized for output
- `validate()` → checks: no negative sizes, no crossed book, prices in 1-99
- Tracks `seq` for gap detection

**`v2/app/replay/engine.py` — ReplayEngine**
- Maintains `dict[str, ReplayBookState]` per ticker
- Reads bronze `orderbook_snapshot` (for initial state) + `orderbook_delta` (for updates) for a date, sorted by `(sid, seq)`
- Processes events in order: snapshots seed the book, deltas modify it
- Snapshot modes: `every_event`, `every_n`, `every_us`, `on_demand` — controls how often the full book state is captured to output
- Alerts on sequence gaps (missing seq numbers)
- Validates book invariants after each delta

**`v2/scripts/replay/build_books.py` — CLI**
```bash
python -m v2.scripts.replay.build_books \
    --date 2026-04-26 \
    --ticker KXNBAPTS-26APR26-JOKIC-O32 \
    --snapshot-mode every_n --snapshot-interval 100 \
    --output snapshots.parquet
```

### Output schema (one row per price level per snapshot)

| Column | Type | Description |
|--------|------|-------------|
| `t_receipt_ns` | int64 | When we received the delta that triggered this snapshot |
| `t_exchange_ns` | int64 | Kalshi's exchange timestamp from the delta |
| `market_ticker` | string | Which ticker this book belongs to |
| `side` | string | "yes" or "no" book |
| `price_cents` | int32 | Price level (1-99 cents) |
| `size` | int32 | Resting contracts at this level |
| `seq` | int32 | Sequence number of the triggering event |

A single book snapshot for one ticker might produce 50-100 rows (one per non-empty price level per side). At `every_n=100` interval, one day of one ticker produces manageable output.

Alternative: one row per snapshot with `yes_levels` and `no_levels` as map columns. TBD — depends on query patterns.

**Status:** Not started

---

## Issue 3: `t_exchange_ns` null on OrderBookUpdate

**Problem:** 100% null on all existing silver BBO data.

**Root cause (two bugs, one fixed):**

1. **`_parse_ts` couldn't parse ISO strings (FIXED).** The `orderbook_delta` channel sends `ts` as an ISO string (`"2026-04-26T00:29:22.803263Z"`), but `_parse_ts` only handled epoch numbers. It tried `float()` on the ISO string, got `ValueError`, and silently returned `None`. Fixed by adding ISO 8601 parsing with microsecond precision.

2. **Deltas silently dropped when no prior snapshot exists (NOT FIXED).** `KalshiTransform` requires a book to already exist (`if book is None: return events`). Many tickers' first appearance in a day's bronze is a delta, not a snapshot, so those deltas are dropped. This means fewer delta-originated BBO rows are produced in the first place.

3. **Snapshots genuinely have no exchange timestamp.** BBO rows from `orderbook_snapshot` events will always have `t_exchange_ns = null` — Kalshi doesn't include a timestamp on snapshots. This is correct behavior, not a bug.

**Current state after fix:** Re-running the backfill (Issue 1) will populate `t_exchange_ns` on delta-originated BBO rows. Snapshot-originated rows will remain null. Some deltas will still be dropped due to bug #2.

**Full fix:** The replay engine (Issue 2) processes deltas directly from bronze, building books from scratch. Every delta has `ts`, so all full-depth snapshots will have exchange timestamps. This makes the BBO silver layer's `t_exchange_ns` gap irrelevant for backtesting.

**Status:** Partially fixed (`_parse_ts`). Fully resolved by Issue 2.

---

## Issue 4: 2.4% crossed books in BBO data

**Problem:** Snapshots where `bid_yes >= ask_yes` (the best bid is at or above the best ask, which should be impossible in a functioning order book — it would mean you could buy and sell simultaneously at a profit).

**Root cause:** Expected during rapid updates. When someone cancels and replaces an order, Kalshi sends two deltas (cancel at old price, place at new price). Between these two deltas, the book is in a transient state that may appear crossed. The BBO silver data captures every intermediate state, including these transient ones.

**Fix:** `ReplayBookState.validate()` will flag *persistent* crossed states (lasting more than N deltas) as real data quality issues. Transient crosses between related deltas (same `ts_ms`) are expected and not errors.

**Status:** Blocked on Issue 2

---

## Issue 5: Limited date coverage (~10 days)

**Problem:** Statistical significance for strategy performance requires more data. ~10 days is enough to build and test the replay engine, but too thin to draw conclusions about strategy edge.

**Fix:** Live ingester is running on EC2. Just needs time. No code changes. NBA playoffs run through mid-June, so data will accumulate daily.

**Status:** Ongoing (passive)

---

## Execution order

The replay engine (Issue 2) is the critical path. It supersedes the BBO silver layer for backtesting — once we have full-depth book reconstruction, the BBO data's limitations don't matter. Re-running the backfill (Issue 1) is nice-to-have for quick BBO queries but not a blocker.

| Step | Issue | What | Needs code? |
|------|-------|------|-------------|
| 1 | #2 | Build `ReplayBookState` + validation | Yes |
| 2 | #2 | Build `ReplayEngine` | Yes |
| 3 | #2 | Build CLI `build_books.py` | Yes |
| 4 | — | Verify: run replay on 4/26 and inspect full-depth output | No |
| 5 | #1 | Re-run backfill for missing dates (optional, improves BBO silver) | No — run existing script |

After step 4, we have full-depth book data and can start strategy development.
