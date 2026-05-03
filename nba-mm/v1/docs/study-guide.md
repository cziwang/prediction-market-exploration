# Study Guide: Kalshi Market Making System

A self-paced guide to understanding how this system works, from high-level
strategy down to deployment. Each section builds on the previous one.
Read the files in order — don't skip ahead.

---

## Level 1: The Business Logic

**Goal:** Understand *what* the bot does and *why* it makes money.

### Reading

1. `docs/strategy-kalshi-mm.md` — **"Why KXNBAPTS"** section only (lines 1–18).
   Answer: why are player-points props more profitable than game-winner markets?

2. `app/strategy/mm.py` — read `MMConfig` (L27–35) and `maker_fee_cents()` (L37–40).
   Answer: what are the five knobs that control the strategy, and how does
   Kalshi's fee formula work?

3. `app/strategy/mm.py` — read `_on_book_update()` (L232–296).
   Trace through a concrete example: bid=40, ask=45 (spread=5), position=0.
   - What is `net_half_spread`?
   - Would the strategy quote? At what prices?
   - Now repeat with position=4 (above `skew_threshold=3`). What changes?

4. `notebooks/strategies/market_making.ipynb` — skim the simulation results.
   This is where the edge was discovered and validated. Note the series
   comparison table and the round-trip P&L charts.

### Exercises

- Calculate `maker_fee_cents(50)`, `maker_fee_cents(10)`, `maker_fee_cents(90)`.
  Verify the symmetry property: `fee(p) == fee(100-p)`.
- Given bid=42, ask=48, position=6, `skew_threshold=3`: what bid and ask
  prices does the strategy post? Does it quote both sides?

---

## Level 2: The Order State Machine

**Goal:** Understand how the strategy prevents double-exposure and ghost orders.

### Reading

1. `app/strategy/mm.py` — read `OrderSideState` (L48–56) and the docstring.
   Draw the state diagram: `idle → pending → resting → cancel_pending → idle`.

2. `app/strategy/mm.py` — read `_maybe_update_side()` (L318–341).
   For each state (`idle`, `pending`, `resting`, `cancel_pending`), trace what
   happens when a new price arrives, when `None` arrives (stop quoting), and
   when the price is unchanged.

3. `app/strategy/mm.py` — read the lifecycle callbacks: `on_order_ack()` (L173),
   `on_cancel_ack()` (L185), `on_fill()` (L191). These are how the state
   machine advances.

4. `tests/strategy/test_mm.py` — read `TestStateMachine` (L54–82).
   The tests are the specification: they prove that two rapid book updates
   don't produce two orders.

### Exercises

- Scenario: the strategy is in state `pending` for ticker X bid side. A new
  `OrderBookUpdate` arrives with a better price. What happens? (Hint: nothing —
  the state machine blocks it.)
- Scenario: the strategy has a resting bid at 40. The book updates to bid=41.
  Trace the state transitions: resting → cancel_pending → idle → pending → resting.
- Why is the `cancel_pending` state necessary? What goes wrong without it?

---

## Level 3: Paper Fill Simulation

**Goal:** Understand how `PaperOrderClient` simulates fills without real orders.

### Reading

1. `app/strategy/mm.py` — read `PaperOrderClient` (L63–136).
   Focus on:
   - `place_limit()` — instant ACK, order tracked in `_resting`
   - `check_fill()` — matching logic: bid fills when `taker_side=="no"`,
     ask fills when `taker_side=="yes"`

2. `app/strategy/mm.py` — read `_on_trade()` (L298–301). This is where
   `check_fill` gets called.

3. `docs/deploy-mm-paper.md` — **"Biases that overstate performance"** section
   (L236–261). These are the five ways paper trading lies to you.

### Exercises

- A trade arrives: ticker=X, `taker_side="no"`, price=40, size=1. The
  strategy has a resting bid at 40 and a resting ask at 45. What happens?
- Same trade, but at price=42 (no resting order at that price). What happens?
- Why does "no queue priority" overstate the fill rate by 2–10x?

---

## Level 4: The Transform Layer

**Goal:** Understand how raw WS frames become typed events.

### Reading

1. `app/events.py` — read all event dataclasses (L15–104). Note:
   - All are `frozen=True` (immutable)
   - All prices are integer cents (never floats)
   - All carry `t_receipt` (wall-clock at ingestion, not server time)

2. `app/transforms/kalshi_ws.py` — read `OrderBookState` (L23–85).
   - How does `from_snapshot()` parse Kalshi's `yes_dollars_fp` arrays?
   - How does `apply_delta()` modify the book?
   - How is `best_ask` derived from the NO book? (Key insight: `ask_yes = 100 - best_no_bid`)

3. `app/transforms/kalshi_ws.py` — read `KalshiTransform.__call__()` (L104–165).
   - What happens on `conn_id` change? (All books invalidated, `BookInvalidated` emitted per ticker)
   - What happens if a delta arrives for a ticker with no snapshot? (Silently skipped)

4. `tests/transforms/test_kalshi_ws.py` — read all tests.
   Pay special attention to `test_conn_id_change_invalidates_books` (L129).

### Exercises

- Kalshi sends `yes_dollars_fp = [["0.4000", "500.00"], ["0.4200", "300.00"]]`.
  What does `OrderBookState.from_snapshot()` produce for `yes_book`?
- The YES book has levels `{40: 500, 42: 300}` and the NO book has
  `{55: 200, 58: 100}`. What are `best_bid`, `best_ask`, and `mid`?
- Why is `conn_id` necessary? What breaks without it? (Hint: read the
  `kalshi_ws/README.md` section on `conn_id` — the 60% negative spreads bug.)

---

## Level 5: The Live Process

**Goal:** Understand how the ingester wires everything together.

### Reading

1. `scripts/live/kalshi_ws/__main__.py` — read `_main()` (L251+).
   Trace the setup:
   - BronzeWriter + SilverWriter as async context managers
   - KalshiTransform creation
   - MMStrategy + PaperOrderClient circular wiring pattern
   - Signal handler registration

2. Same file — read `Ingester._archive()` (L203+). This is the hot path:
   ```
   raw frame → bronze.emit() → transform() → [strategy.on_event() + silver.emit()] → drain pending_events
   ```

3. Same file — read `Ingester.run()` (L134+) and `_connect_once()` (L158+).
   Understand the reconnect loop with exponential backoff.

4. Same file — read `_sign()` (L71) and `_build_auth_headers()` (L86).
   Kalshi requires RSA-PSS signed headers even for public data channels.

### Key patterns to understand

- **Async context managers**: `async with bronze, silver:` ensures flush-on-exit.
  Both writers start background `_flush_loop` tasks in `__aenter__` and drain
  buffers in `__aexit__`.
- **Signal handling**: `loop.add_signal_handler(SIGINT, ingester.shutdown)` registers
  a synchronous callback. `shutdown()` sets an `asyncio.Event` that unblocks
  the reconnect loop's `wait_for`.
- **Circular wiring**: Strategy and PaperOrderClient reference each other.
  Constructed by creating strategy first (with `client=None`), then client,
  then back-patching `strategy._client`.
- **`asyncio.to_thread`**: S3 PUT calls are blocking I/O offloaded to a thread
  pool so the event loop stays responsive.

### Exercises

- Trace a single WS frame from `async for raw in ws` through every function
  it touches until it's written to S3. List each function in order.
- What happens when the WS disconnects? Trace the reconnect path.
- What happens when you Ctrl-C the process? Trace the shutdown path.
- Why is `_fetch_open_tickers_by_series` called via `asyncio.to_thread`?

---

## Level 6: Bronze and Silver Storage

**Goal:** Understand the two-tier storage architecture.

### Reading

1. `app/services/bronze_writer.py` — read the full file.
   Key mechanics:
   - Buffers are `dict[channel, list[bytes]]` — one list per channel
   - Flush triggers: 5 MB size OR 60s timer OR shutdown
   - Writes gzip-compressed JSONL to `bronze/{source}/{channel}/YYYY/MM/DD/HH/{uuid}.jsonl.gz`
   - Buffer swap is atomic (single-threaded event loop, no await between read and reset)

2. `app/services/silver_writer.py` — read the full file.
   Key differences from bronze:
   - Buffers typed `Event` objects, not bytes
   - Groups by `type(event).__name__` (e.g., `"OrderBookUpdate"`)
   - Serializes via `dataclasses.asdict()` + PyArrow at flush time
   - Writes zstd-compressed Parquet
   - Flushes every 60s (not size-triggered)

3. `docs/data-flow.md` — reread now with code-level understanding.
   The "one transform execution site" principle should now be concrete:
   the transform runs once in `_archive()`, and its output feeds both
   the strategy and silver simultaneously.

### Exercises

- During a live game, the delta channel produces 233 frames/sec. How many
  S3 PUTs per minute does BronzeWriter make for that channel? (Answer: ~1,
  because the 60s timer fires before the 5MB threshold.)
- Why does SilverWriter use `dataclasses.asdict()` instead of storing raw dicts?
- Why is bronze gzip-JSONL while silver is Parquet? What are the tradeoffs?
- What does `v=1` in the silver path mean? When would it change?

---

## Level 7: The Test Suite

**Goal:** Understand how correctness is verified at each layer.

### Reading

1. `tests/transforms/test_kalshi_ws.py` — 7 transform tests.
   Note the inline fixture pattern: `_snapshot_frame()`, `_delta_frame()`,
   `_trade_frame()` build raw WS frames from parameters.

2. `tests/strategy/test_mm.py` — 16 strategy tests across 7 test classes.
   Covers: quoting decisions, state machine, position limits, skewing,
   fills, book invalidation, fee formula, and quote event deduplication.

3. `tests/integration/test_paper_e2e.py` — 2 integration tests.
   `_build_fixture_frames()` constructs a realistic 6-frame session.
   `test_e2e_paper_trading` verifies the full pipeline: transform → strategy →
   fills → position tracking.
   `test_e2e_connection_change` verifies `conn_id` transitions trigger
   order cancellation and re-posting.

### Run the tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

All 32 tests should pass. Read any failures carefully — they tell you
exactly what invariant was violated.

### Exercises

- Add a test for a new scenario: what happens when `max_aggregate_position`
  is hit across two different tickers? (Look at `TestPositionLimits` for
  the pattern.)
- Add a test that verifies `MMFillEvent.maker_fee` is correctly computed
  for a fill at price=30.

---

## Level 8: Deployment

**Goal:** Understand how the system runs in production.

### Reading (in order)

1. `docs/ec2-bootstrap.md` — one-time EC2 setup (instance type, IAM role,
   Python venv, AWS CLI).

2. `docs/live-kalshi-ws-service.md` — systemd unit file for the Kalshi
   ingester. Key settings: `KillSignal=SIGINT` (clean shutdown),
   `TimeoutStopSec=30` (drain window), `Restart=always`.

3. `docs/live-nba-cdn-service.md` — same pattern for the NBA poller.

4. `docs/deploy-mm-paper.md` — activating the strategy with `MM_ENABLED=1`.
   Read the monitoring section: what log patterns to watch, what red flags
   to look for.

5. `docs/deploy-mm-live.md` — Phase 2 (not yet implemented). Read the
   architecture change table to understand what's different about live
   trading vs paper.

### Exercises

- What systemd command do you run to enable paper trading on EC2?
- What happens if the process crashes? How does systemd respond?
  How does the ingester respond on restart?
- What's the maximum data loss window on a crash? (Answer: one
  BronzeWriter flush interval — ~60 seconds.)

---

## Level 9: Phase 2 Design (Advanced)

**Goal:** Understand the live trading design that hasn't been built yet.

### Reading

1. `docs/strategy-kalshi-mm.md` — full document, focusing on:
   - Phase 2 architecture diagram (WS push channels)
   - `KalshiOrderClient` design (REST placement, WS ACKs)
   - `client_order_id` correlation flow
   - Circuit breaker pattern
   - `bootstrap()` startup recovery
   - Reconciliation loop
   - Resolved issues table (C1–C5, M1–M7)

2. `app/clients/kalshi_rest_orders.py` — if it exists, read the REST
   order client skeleton.

### Key concepts

- **REST for writes, WS for reads**: Kalshi has no WS order submission.
  Orders go out via REST; confirmations come back via WS push channels.
- **`client_order_id` correlation**: UUID generated at placement time,
  echoed back in the `user_orders` WS ACK. Bridges the gap between
  "I sent a REST request" and "Kalshi confirmed my order."
- **Authoritative vs inferred fills**: Phase 1 infers fills from the public
  trade stream (unreliable — queue priority, misattribution). Phase 2 gets
  authoritative fills from the `fill` WS channel.

---

## Suggested Learning Path

| Day | Focus | Time |
|---|---|---|
| 1 | Levels 1–2 (strategy + state machine) | 2–3 hours |
| 2 | Level 3 (paper fills) + Level 4 (transforms) | 2–3 hours |
| 3 | Level 5 (live process) + Level 6 (storage) | 2–3 hours |
| 4 | Level 7 (tests) — run them, read them, add one | 1–2 hours |
| 5 | Levels 8–9 (deployment + Phase 2 design) | 1–2 hours |

After completing all levels, you should be able to:
- Trace a single WS frame from the wire through every layer to S3
- Explain why the strategy quotes at a specific price for a given book state
- Predict what happens on reconnect, shutdown, or position limit hit
- Add a new test for any component
- Deploy or roll back the paper trading strategy on EC2
