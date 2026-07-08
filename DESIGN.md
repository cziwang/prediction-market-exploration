# Design Doc: NBA Prediction Market Trading Infrastructure

**Author:** cziwang
**Last updated:** 2026-07-07

---

## Motivation

Kalshi operates a prediction market for NBA game outcomes. Markets for game winners (KXNBAGAME), point totals (KXNBATOTAL), and spreads (KXNBASPREAD) remain open and actively traded throughout live games. The hypothesis is that these markets are slow to reprice after significant in-game events — scoring runs, lead changes, foul trouble — creating windows where a model with access to live play-by-play data can identify mispriced contracts.

Exploiting this requires solving a stream processing problem: two independent data sources — Kalshi WebSocket market data and NBA CDN play-by-play polls — must be joined in real time to produce an enriched event stream that captures both the market price and the game state at every point in time. This enriched stream is the foundation for signal generation, backtesting, and eventually live execution.

The NBA season is currently over. All data collected (59 playoff games, April–May 2026) is historical and stored in S3. The infrastructure must support both **batch processing of historical data** and **live streaming during the season** using the same pipeline and business logic. This motivates a **Kappa architecture**: one pipeline, one topology, where historical data is replayed through Kafka and processed by the exact same Flink job that will consume live feeds.

### A note on scale

This stack — Flink, Kafka, ClickHouse, Ray, Redis — is roughly 10x the operational surface this data volume requires. Fifty-nine games of playoff data fits in DuckDB on a laptop. The stack is chosen deliberately as a learning vehicle for production trading-infrastructure patterns: event-time stream processing, exactly-once semantics, order lifecycle management, and fault-tolerant state. The design aims to get these patterns *correct*, not merely present.

---

## Background

### Data collected

During the 2025-26 NBA playoffs, two data sources were collected continuously from an EC2 instance and written to S3:

**Kalshi WebSocket feed** — raw market data frames, flushed to S3 bronze every 60s or 5MB:
```
s3://prediction-markets-data/bronze/kalshi_ws/
    orderbook_delta/YYYY/MM/DD/HH/{uuid}.jsonl.gz
    orderbook_snapshot/YYYY/MM/DD/HH/{uuid}.jsonl.gz
    trade/YYYY/MM/DD/HH/{uuid}.jsonl.gz
```

Every Kalshi record carries **two timestamps:**
- `t_receipt` — when our WebSocket client received the frame (float seconds, assigned by our EC2 host clock via chrony/AWS Time Sync)
- `frame.msg.ts` / `frame.msg.ts_ms` — when the event occurred on Kalshi's exchange (exchange clock)

**NBA CDN play-by-play** — polled every ~20s per active game, flushed similarly:
```
s3://prediction-markets-data/bronze/nba_cdn/live_pbp/YYYY/MM/DD/HH/{uuid}.jsonl.gz
```

Every NBA CDN record also carries **two timestamps:**
- `t_receipt` — when our HTTP poller received the response (our host clock)
- `frame.timeActual` — when the action actually happened on the court (NBA's clock)

The gap between these two timestamps is the **information delay** — the time between something happening in the real world and our system learning about it. This gap is the central challenge of the entire design (see §The Two-Timestamp Problem).

**Merged bronze** (deduplicated, one file per game / channel-date):
```
s3://prediction-markets-data/bronze_merged/
    nba_cdn/nba_pbp_{game_id}.jsonl.gz     # 59 games, ~500-800 actions each
    kalshi_ws/trade/date=YYYY-MM-DD/merged.jsonl.gz
    kalshi_ws/orderbook_delta/date=YYYY-MM-DD/merged.jsonl.gz
    kalshi_ws/orderbook_snapshot/date=YYYY-MM-DD/merged.jsonl.gz
```

**Reference:**
```
s3://prediction-markets-data/reference/kalshi_markets.parquet
    # 117,900 NBA markets, 113,613 with settlement results
```

### Current limitations

- No unified view of game state + market price at a given timestamp
- PBP and Kalshi data are joined ad-hoc in notebooks — not reproducible, not production-grade
- No tick store — every query re-reads S3 Parquet
- No backtesting or live execution infrastructure

---

## Goals

1. **Unified enriched tick stream** — for every Kalshi trade, a record containing the concurrent game state via a *deterministic, event-time-correct* as-of join, with both receipt-time and event-time views
2. **Batch/streaming parity** — one Flink topology; historical replay and live feeds produce byte-identical output for identical input, verified by a golden-game fixture
3. **High-throughput tick store** — ClickHouse for enriched ticks, sub-second analytical queries across full history
4. **Distributed backtesting** — Ray-based parameter sweep over ClickHouse data, reproducible results
5. **Production-grade live stack** — OMS with crash recovery, risk engine, fault-tolerant state

## Non-goals

- Co-location or FPGA latency optimisation
- Multi-exchange arbitrage
- Real-time ML inference (analytical model suffices initially)
- Multi-shard / replicated ClickHouse (single node + backups)

---

## The Two-Timestamp Problem

This is the intellectual core of the design. Every record in the system carries two timestamps, and joining on the wrong one produces a backtest that lies.

### Concrete example

```
Real world (event time):
  7:59:50  Brunson hits a 3-pointer. NYK 100→103, ATL 98. Score diff: +2 → +5.
  8:00:00  A trader on Kalshi buys KXNBAGAME-NYK at 72¢.

Our system (receipt time):
  7:59:30  Our CDN poller received the PREVIOUS game state (score diff +2)
  8:00:00  Our WS client receives the Kalshi trade (72¢)
  8:01:00  Our CDN poller FINALLY picks up the Brunson 3-pointer (score diff +5)
```

At 8:00:00, when the trade arrives, **our system does not know about the 3-pointer.** The CDN hasn't been polled yet. Our model would compute win probability using `score_diff=2`, not `score_diff=5`. The Kalshi trader who bought at 72¢ might have been reacting to the 3-pointer from a faster feed — but our system couldn't have known that.

### Two views of the same trade

**Receipt-time view ("what did we know?"):**
```
Trade at 8:00:00 → joined to game state received at 7:59:30 (score_diff=2)
→ Model: P(win) = 0.58
→ Edge: 0.58 - 0.72 = -0.14. We would NOT buy — the market looks expensive.
```

**Event-time view ("what had actually happened?"):**
```
Trade at 8:00:00 → joined to Brunson 3PT at 7:59:50 (score_diff=5)
→ Model: P(win) = 0.71
→ Edge: 0.71 - 0.72 = -0.01. Negligible edge — market was correctly priced.
```

These are both correct — they answer different questions:

| Join key | Question | Use case |
|----------|----------|----------|
| `t_receipt` | "What would my system have done with the information it actually had?" | **Realistic backtest** — this is what you'd experience live |
| `timeActual` / `ts_ms` | "What would a system with zero information delay have done?" | **Theoretical upper bound** — measures how much polling lag costs you |

### Why you need both

The receipt-time view is your realistic backtest. The event-time view is the oracle. **The difference between them is your information delay**, and it's directly measurable:

```sql
SELECT
    avg(receipt_score_diff != event_score_diff) AS pct_trades_with_stale_state,
    avg(receipt_info_delay_ms)                  AS avg_delay_ms,
    percentile(receipt_info_delay_ms, 0.95)     AS p95_delay_ms
FROM enriched_trades
WHERE series = 'KXNBAGAME'
```

If 30% of trades have stale game state and the average delay is 15 seconds, you know exactly how much edge your polling lag is costing you. This is also the basis for deciding whether faster data sources (e.g. a real-time NBA stats API) are worth the cost.

### How this shapes the Flink join

The enrichment operator maintains **two** game-state views per key, updated as buffered records are finalized in timestamp order:

```
receipt view    ← latest game state by t_receipt_ns (what our system knew)
event view      ← game state with max t_event_ns among those processed
                  (what had actually happened on court, per best knowledge)
```

On each trade, the operator emits one row carrying **both** views. The watermark and buffering logic operates on `t_receipt` (since receipt time is the clock our system actually runs on). The `timeActual`/`ts_ms` fields are carried as payload and used to populate the event-time columns. Full buffering semantics in D1.

---

## Key Design Decisions

### D1. As-of join via `KeyedCoProcessFunction`, not interval join

An interval join is wrong for this problem twice over:

**Problem 1 — multiple matches:** An interval join emits one row per matching game state in the window. If 3 game state updates happened in the 60s before a trade, you get 3 output rows for that one trade. You wanted exactly one — the latest.

**Problem 2 — silent drops:** It is an inner join. A trade with no game state in the window (pregame, halftime, CDN outage) produces zero output. That trade disappears from the dataset. You never notice it's gone. Silent row loss in the canonical tick store is unacceptable.

**Solution:** A `KeyedCoProcessFunction` keyed by `game_id`, buffering **both** streams:

- **Both sides buffered:** game states *and* trades are held in keyed state until the watermark passes their `t_receipt_ns`; an event-time timer is registered per buffered timestamp
- **On timer fire:** process all buffered records with `t_receipt_ns ≤ watermark` **in timestamp order** — game states update the two view states (receipt view: latest by `t_receipt_ns`; event view: max `t_event_ns` seen), trades snapshot the views at that instant and emit
- **Tie at the same timestamp:** the game state applies before the trade ("state as of t" includes updates at t)
- **No game state available:** emit anyway with null game-state fields. Downstream filters on staleness; the pipeline never drops
- **Staleness fields:** every enriched record carries `r_info_delay_ms` and `e_info_delay_ms` so consumers can filter stale joins

**Why buffer *both* streams, not just trades?**

Buffering trades handles network reordering: a trade at T must wait until the watermark confirms all earlier game states have arrived. But a "keep only the latest game state" design has a subtler future-leak: if states S₁(t=1) and S₅(t=5) both arrive *before* the watermark passes a buffered trade T₃(t=3), "latest" is S₅ — enriching T₃ with S₅ uses information from *after* the trade. Processing buffered records in timestamp order enriches T₃ with S₁, exactly what a live system knew at t=3. (Caught by `test_no_future_leak` during implementation of `pm.enrich.join`.)

This is what makes the join **deterministic**: output depends on timestamps and watermark positions, never on arrival order. Batch replay and live streaming produce identical results for identical input — verified by a Hypothesis property test over arbitrary arrival permutations.

**Implementation note:** the join logic lives in `pm/enrich/join.py` as a pure-Python `AsOfJoiner` class with zero Flink imports (`on_game_state` / `on_trade` / `advance_watermark`). The Flink operator is a thin shell that persists this state and wires `advance_watermark` to event-time timers. The hard semantics are unit-tested in milliseconds; Flink contributes distribution, state persistence, and timer plumbing.

### D2. One pipeline: replay through Kafka is the only path

Historical processing works by replaying bronze data through Kafka with original event timestamps — the one Flink job consumes it, indistinguishable from live data. There is no separate `FileSource`-based batch path.

Why: a batch path reading S3 directly would have different split ordering and watermark generation than the Kafka path. Two pipelines whose parity must then be proven is exactly the problem Kappa exists to eliminate. One topology, one watermark config, one test.

### D3. Delivery semantics: at-least-once + idempotent sink

Flink checkpoint recovery means **reprocessing**, which produces **duplicates** downstream — not data loss. ClickHouse's MergeTree does not deduplicate logically identical rows across different insert batches.

Chosen strategy:

- **ClickHouse sink:** at-least-once inserts, with `insert_deduplication_token` derived deterministically from `(checkpoint_id, subtask_id)`. After recovery, re-inserted batches carry the same token and are dropped by ClickHouse
- **Kafka sink (`enriched.trades`):** plain at-least-once. Consumers (strategy service) deduplicate on `(market_ticker, t_trade_ns)`
- **Why not transactional exactly-once?** Transaction-timeout-vs-checkpoint-interval tuning is real operational cost with no correctness gain over idempotent consumption at this scale. Documented trade-off, not an omission

### D4. Reference data via broadcast state

The ticker→game_id mapping (e.g. `KXNBAGAME-26APR18ATLNYK-NYK` → `0042500121`) is a low-volume stream, not a static file. A `reference.markets` Kafka topic is connected via `broadcast()` to the enrichment operator. New markets become joinable without restart, the mapping participates in checkpoints, and there is no batch-vs-streaming special case. The replayer seeds this topic from `reference/kalshi_markets.parquet`.

### D5. ClickHouse: plain MergeTree, batched inserts

- **Plain `MergeTree`**, single node, nightly `clickhouse-backup` to S3
- **Insert batching:** the Flink sink flushes per checkpoint (60s) or 10K rows, whichever first. Small frequent inserts are ClickHouse's canonical anti-pattern (too many parts)
- **No table partitioning** at this volume (~tens of millions of rows); `ORDER BY` covers the query pattern
- **`DateTime64` materialized columns** alongside raw `Int64` nanos so ad-hoc queries are humane

### D6. Redis is a cache; SQLite is the source of truth

The OMS's SQLite event log is the **single source of truth** for orders, fills, and derived positions. Fills are written to SQLite *before* Redis is updated — this write ordering is what makes recovery sound. Redis holds only rebuildable caches: latest game state, latest mids, and a position mirror for fast reads. On boot, Redis is rebuilt from SQLite. If Redis dies mid-session, the risk engine circuit-breaks (cancel all) and state is rebuilt from SQLite.

### D7. Kafka is the data plane; gRPC is control plane only

The strategy service is a **Kafka consumer** of `enriched.trades`. Kafka *is* the backpressure mechanism: consumer lag is visible, bounded, and monitorable. gRPC is used only for control-plane operations: status queries, config changes, manual kill switch. No market data flows through gRPC.

### D8. Every event carries `schema_version`; malformed records go to a DLQ

Every event has an explicit `schema_version: int`. Flink routes unknown versions and parse failures to a dead-letter topic (`dlq.{source_topic}`) with the raw bytes and error. A malformed bronze record must neither crash-loop the job nor vanish silently. Schema registry (Avro/Protobuf) is noted as future work.

### D9. Quantities as centi-contracts

Kalshi's wire format uses fractional contracts — `count_fp: "2.58"`, `delta_fp: "-0.05"`. Rounding to whole contracts would corrupt book reconstruction (a `-0.05` delta becomes `0`). All quantities in the normalized event layer are therefore **centi-contracts**: the raw fractional value × 100, rounded to an integer. `"2.58"` → `258`, `"-0.05"` → `-5`. This preserves precision while keeping the "integer only, no floats for money or quantity" invariant.

---

## Architecture Overview

```
                 ┌────────────────────────────────────┐
                 │            DATA SOURCES            │
                 │                                    │
                 │  S3 bronze ──► Replayer (batch)    │
                 │  Kalshi WS ──► Normalizer (live)   │
                 │  NBA CDN  ───► Normalizer (live)   │
                 └───────────────┬────────────────────┘
                                 │  (single entry point)
                 ┌───────────────▼────────────────────┐
                 │              KAFKA                 │
                 │  kalshi.trades      (4 partitions, │
                 │  kalshi.book_update  key=ticker)   │
                 │  nba.game_state     (1 partition,  │
                 │                      key=game_id)  │
                 │  reference.markets  (1 partition)  │
                 │  dlq.*                             │
                 └───────────────┬────────────────────┘
                                 │
                 ┌───────────────▼────────────────────┐
                 │          APACHE FLINK              │
                 │  KeyedBroadcastProcessFunction     │
                 │   · dual-view as-of join (D1)     │
                 │   · broadcast reference state (D4) │
                 │   · feature computation            │
                 │   · DLQ routing (D8)               │
                 └──────┬──────────────┬──────────────┘
                        │              │
        ┌───────────────▼───┐   ┌──────▼───────────────┐
        │    CLICKHOUSE     │   │ KAFKA                │
        │  enriched_trades  │   │ enriched.trades      │
        │  (MergeTree, D5)  │   └──────┬───────────────┘
        └────────┬──────────┘          │ (Kafka consumer, D7)
                 │              ┌──────▼───────────────┐
        ┌────────▼─────────┐   │  STRATEGY SERVICE    │
        │       RAY        │   └──────┬───────────────┘
        │ distributed      │          │ order intents
        │ backtests        │   ┌──────▼───────────────┐
        └──────────────────┘   │  OMS ── SQLite (SoT) │──► Kalshi REST
                               └──────┬───────────────┘
                               ┌──────▼───────────────┐
                               │  RISK ENGINE         │
                               │  Redis (cache, D6)   │
                               └──────────────────────┘
```

---

## Component Design

### 1. Historical Event Replayer

Reads S3 bronze, publishes to Kafka with **original `t_receipt` timestamps**, making historical data indistinguishable from live data to Flink.

**Ordering guarantees (required, not assumed):**

Kafka preserves order only within a partition. The replayer therefore:

1. **Merge-sorts across bronze files** by `t_receipt_ns` before producing. Bronze files flushed on 60s/5MB boundaries are *probably* internally ordered, but cross-file order is not guaranteed. Out-of-order records within a source abort the replay with a data-quality report — the guarantee is verified, not assumed
2. **Partitions by the same key Flink uses**: `kalshi.trades` and `kalshi.book_update` keyed by `market_ticker`, `nba.game_state` keyed by `game_id` — so per-key order in Kafka matches per-key order in Flink state
3. **Carries both timestamps:** the Kafka message timestamp is `t_receipt_ns // 1_000_000` (receipt time, for Flink watermarks). The event time (`timeActual` / `ts_ms`) is inside the message payload and passed through unchanged

**Partition counts:** 4 for trade/book topics (matches Flink parallelism: 2 TaskManagers × 2 slots; sufficient for ~7 trades/sec peak throughput), 1 for game_state (≤15 concurrent games, ~1 event/sec — a single partition eliminates idle-partition watermark stalls entirely), 1 for reference. Partition count can be increased later if throughput grows; it cannot be decreased.

**Completion:** emits a bounded-marker record per partition when a topic's replay is complete; the Flink source treats these as end-of-input in bounded mode.

```python
class EventReplayer:
    def replay(
        self,
        sources: list[BronzeSource],
        speed_multiplier: float = 0.0,   # 0.0 = as fast as possible, 1.0 = real time
    ) -> ReplayStats: ...
```

`speed_multiplier=1.0` exists for integration tests: replaying a game in real time against the full live stack.

### 2. Normalisation Layer

Pydantic v2 models, all frozen, all carrying `schema_version`. Every normalized event carries both timestamps where applicable:

```python
class MarketEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: int = 1
    t_receipt_ns: int                        # when our system received this
    source: Literal["kalshi_ws", "nba_cdn"]

class TradeEvent(MarketEvent):
    market_ticker: str
    price_cents: int                         # YES price, integer cents
    size_cc: int                             # centi-contracts (D9)
    taker_side: Literal["yes", "no"]
    t_exchange_ns: int | None                # when Kalshi's exchange matched this trade

class BookUpdateEvent(MarketEvent):
    market_ticker: str
    side: Literal["yes", "no"]
    price_cents: int
    delta_cc: int                            # signed centi-contracts (D9)
    seq: int                                 # per-subscription sequence number
    sid: int                                 # subscription id

class GameStateEvent(MarketEvent):
    game_id: str
    period: int
    clock_seconds: float                     # seconds remaining in current period
    seconds_remaining: float                 # total seconds remaining in regulation
    score_home: int
    score_away: int
    score_diff: int                          # score_home - score_away
    action_type: str
    t_event_ns: int | None                   # when this action happened on court (frame.timeActual)
```

**DLQ handling:** parse failures at the normalisation boundary produce `Dlq(raw_bytes, error, context)` — never exceptions that kill the consumer, never silent drops.

**Clock discipline:** `t_receipt_ns` is assigned by the collector host; the EC2 instance runs chrony (AWS Time Sync at `169.254.169.123`). All watermark and join semantics depend on this.

### 3. Flink Enrichment Job

**Topology:**

```
KafkaSource(kalshi.trades) ──────┐ keyed: game_id (via broadcast ref lookup)
                                 ├──► KeyedBroadcastProcessFunction ──► ClickHouse sink
KafkaSource(nba.game_state) ─────┤         (dual-view as-of join)    └─► KafkaSink(enriched.trades)
                                 │
KafkaSource(reference.markets) ──┘ broadcast (D4)
```

**Watermarks — asymmetric per source:**

| Stream | Out-of-orderness bound | Rationale |
|--------|----------------------|-----------|
| `kalshi.trades` | 2s | Kalshi WS is near-real-time |
| `nba.game_state` | 30s | Matches CDN poll interval (~20s + network jitter) |
| Both | `.withIdleness(Duration.ofSeconds(60))` | With ≤15 concurrent games, most `nba.game_state` partitions are idle at any moment. Idle partitions that don't advance watermarks stall every event-time timer in the join |

Watermarks operate on `t_receipt_ns` — receipt time is the clock our system runs on.

Late events (beyond the bound) are counted in a `late_data` metric per stream and routed to the DLQ with reason `late`.

**Dual-view join operator (D1 + Two-Timestamp Problem):**

Per-key state (`game_id`):

```
ValueState<GameStateEvent>  latest_by_receipt   — latest by t_receipt_ns
ValueState<GameStateEvent>  latest_by_event     — latest by t_event_ns
MapState<Long, List<Trade>> pending_trades      — buffered until watermark passes
```

On `GameStateEvent` arrival:
- If `event.t_receipt_ns > latest_by_receipt.t_receipt_ns` → update `latest_by_receipt`
- If `event.t_event_ns > latest_by_event.t_event_ns` → update `latest_by_event`

On `TradeEvent` arrival:
- Buffer in `pending_trades` keyed by `t_receipt_ns`
- Register event-time timer at `t_receipt_ns`

On timer fire (watermark ≥ trade's `t_receipt_ns`):
- Enrich trade with `latest_by_receipt` → receipt-time columns
- Enrich trade with `latest_by_event` → event-time columns
- Emit. If no game state exists for either view, emit with nulls + staleness flags

State size per key: two game-state records + the in-flight trade buffer (bounded by watermark lag, typically a few seconds of trades).

**Enriched output:**

```python
class EnrichedTrade(BaseModel):
    schema_version: int = 1

    # --- Trade fields ---
    t_trade_receipt_ns: int                   # when our system received this trade
    t_trade_exchange_ns: int | None           # when Kalshi matched this trade
    market_ticker: str
    series: str
    trade_price: int                          # YES price, cents
    trade_size_cc: int                        # centi-contracts (D9)
    taker_side: str

    # --- Receipt-time view ("what did we know?") ---
    game_id: str
    r_period: int | None                      # game state per latest received update
    r_seconds_remaining: float | None
    r_score_diff: int | None
    r_model_prob: float | None                # Φ(score_diff / √(0.44 · secs_remaining))
    r_edge: float | None                      # r_model_prob - market_implied_prob
    r_info_delay_ms: int | None               # trade_receipt - game_state_receipt (ms)

    # --- Event-time view ("what had actually happened?") ---
    e_period: int | None                      # game state per latest on-court event
    e_seconds_remaining: float | None
    e_score_diff: int | None
    e_model_prob: float | None
    e_edge: float | None
    e_info_delay_ms: int | None               # trade_exchange - game_event_actual (ms)

    # --- Common ---
    market_implied_prob: float                 # trade_price / 100
    settlement_result: int | None             # 1=YES, 0=NO, None=unsettled (broadcast ref)
```

**Why both views matter:**

- **Backtesting uses the receipt-time view** (`r_*` columns). This is what your system would actually experience live. A backtest built on event-time data systematically overstates edge — your model appears to react to information it didn't yet have
- **The event-time view** (`e_*` columns) is the oracle. It shows what a system with zero information delay would have computed
- **The difference** (`r_edge - e_edge`, `r_info_delay_ms`) quantifies the cost of polling lag and directly answers "would a faster data source be worth paying for?"

```sql
-- How often is our game state stale when a trade arrives?
SELECT
    count(*) AS total_trades,
    avg(r_score_diff != e_score_diff) AS pct_stale,
    avg(r_info_delay_ms) / 1000.0 AS avg_delay_sec,
    quantile(0.95)(r_info_delay_ms) / 1000.0 AS p95_delay_sec
FROM enriched_trades
WHERE series = 'KXNBAGAME'

-- How much edge are we losing to information delay?
SELECT
    avg(abs(e_edge) - abs(r_edge)) AS avg_edge_lost_to_delay
FROM enriched_trades
WHERE series = 'KXNBAGAME'
AND e_edge IS NOT NULL AND r_edge IS NOT NULL
```

**Fault tolerance:** checkpoints to S3 every 60s. Recovery reprocesses from the last checkpoint; duplicates are absorbed by the ClickHouse dedup token (D3).

### 4. ClickHouse Tick Store

```sql
CREATE TABLE enriched_trades (
    -- Trade
    t_trade_receipt_ns     Int64,
    t_trade_receipt        DateTime64(3) MATERIALIZED fromUnixTimestamp64Nano(t_trade_receipt_ns),
    t_trade_exchange_ns    Nullable(Int64),
    schema_version         UInt8,
    market_ticker          LowCardinality(String),
    series                 LowCardinality(String),
    game_id                String,
    trade_price            Int32,
    trade_size_cc          Int32,
    taker_side             LowCardinality(String),

    -- Receipt-time view
    r_period               Nullable(Int32),
    r_seconds_remaining    Nullable(Float32),
    r_score_diff           Nullable(Int32),
    r_model_prob           Nullable(Float32),
    r_edge                 Nullable(Float32),
    r_info_delay_ms        Nullable(Int64),

    -- Event-time view
    e_period               Nullable(Int32),
    e_seconds_remaining    Nullable(Float32),
    e_score_diff           Nullable(Int32),
    e_model_prob           Nullable(Float32),
    e_edge                 Nullable(Float32),
    e_info_delay_ms        Nullable(Int64),

    -- Common
    market_implied_prob    Float32,
    settlement_result      Nullable(Int8)
) ENGINE = MergeTree
ORDER BY (series, game_id, t_trade_receipt_ns)
```

Per D5: single node, no table partitioning, batched inserts with `insert_deduplication_token = f"{job_id}-{checkpoint_id}-{subtask_id}"`, nightly backup to S3.

`LowCardinality(String)` on `market_ticker`, `series`, `taker_side` — ClickHouse encodes these as integer dictionaries for ~3-5x compression and faster groupby. `ORDER BY (series, game_id, t_trade_receipt_ns)` matches the dominant query pattern: filter by series, then game, then time range.

### 5. Redis (cache only, per D6)

```
game_state:{game_id}      → JSON   latest GameStateEvent (sub-ms feature reads)
mid:{market_ticker}       → Int    latest midpoint (mark-to-market)
position:{market_ticker}  → Int    mirror of SQLite-derived positions
```

All keys rebuildable from SQLite + Kafka on boot. No durability assumptions.

### 6. Order Management System

**State machine:**

```
NEW ──► SENT ──► ACKNOWLEDGED ──► PARTIALLY_FILLED ──► FILLED
                              └──► CANCELLED
                              └──► REJECTED
```

Invalid transitions raise and are logged. Every transition appends to the SQLite event log — the system of record (D6).

**Exactly-once placement:**

1. OMS assigns a `client_order_id` (UUID4) and writes `INTENT` to SQLite *before* any network call
2. Order is sent to Kalshi carrying that `client_order_id`. **Kalshi supports client-supplied order IDs** (required in CreateOrderV2). Duplicate submissions are *rejected with an error*, so a retry after an ambiguous timeout that hits "duplicate client_order_id" means the original send succeeded — the OMS responds by fetching actual order state via GetOrder, never by treating it as a failure
3. **Startup reconciliation** — this is the core of the OMS, not an appendix:
   a. Replay SQLite log → expected order states
   b. Query Kalshi REST: open orders + fills since last known event
   c. Diff: orders with `INTENT`/`SENT` but unknown to exchange → mark failed; orders alive on exchange but terminal locally → adopt and reconcile; fills that arrived while down → apply to positions
   d. Only after reconciliation converges does the OMS accept new intents

### 7. Risk Engine

Consumes fill events (from OMS, via SQLite-then-Redis per D6 write ordering), maintains positions and P&L, enforces limits:

| Limit | Default | Action on breach |
|-------|---------|------------------|
| Max position per ticker | 10 contracts | Block order intent |
| Max aggregate position | 100 contracts | Block all intents |
| Max drawdown | $50 | Circuit breaker → cancel all |
| Max order size | 10 contracts | OMS rejects at send |

Mark-to-market from `mid:{ticker}` in Redis. **Sequence-gap guard:** the book maintained from `BookUpdateEvent` tracks `(sid, seq)`; a gap triggers book invalidation → mid marked stale → if stale > 30s, positions in that ticker are marked unpriceable and the risk engine blocks new intents until a snapshot resync.

### 8. Strategy Service

A Kafka consumer of `enriched.trades` (D7). Deduplicates on `(market_ticker, t_trade_receipt_ns)` (at-least-once delivery, D3). Emits order intents to the OMS.

```python
class Strategy(ABC):
    @abstractmethod
    def on_enriched_trade(self, event: EnrichedTrade) -> OrderIntent | None: ...
    @abstractmethod
    def on_fill(self, fill: Fill) -> None: ...
```

**Live/backtest parity:** the backtest runner replays `EnrichedTrade` rows from ClickHouse through the same `on_enriched_trade`. Zero strategy-code changes. Backtests use the `r_*` (receipt-time) columns by default — the strategy sees the same information it would have seen live.

### 9. Ray Distributed Backtesting

Parameter sweep over ClickHouse data. Results immutable in `s3://prediction-markets-data/backtests/{run_id}/` with config, fills, PnL series, metrics.

```python
@ray.remote
class BacktestWorker:
    def run(self, config: BacktestConfig) -> BacktestResult:
        data = clickhouse.query("""
            SELECT * FROM enriched_trades
            WHERE series = 'KXNBAGAME'
            ORDER BY t_trade_receipt_ns
        """)
        strategy = AnalyticalStrategy(config)
        return BacktestEngine(strategy).run(data)
```

---

## Testing Strategy

### Golden game fixture (the parity anchor)

One game — `0042500121` (NYK 113–102 ATL, 2026-04-18; hand-verified against ESPN/Basketball-Reference box scores) — is the canonical fixture:

- Its bronze data is frozen as a test asset (`tests/fixtures/golden/`)
- Its enriched output is **hand-verified once** — spot-check joins at known moments: tip-off, Brunson's first 3PT, a lead change, final buzzer. Both receipt-time and event-time views verified
- **Every change** to the pipeline must reproduce the golden output byte-for-byte (modulo explicitly versioned schema changes)
- Batch replay and real-time-speed replay must produce identical golden output — this is what makes the Kappa parity claim testable

### Other layers

- **Property-based tests (Hypothesis):** order book invariants (never crossed after any delta sequence), OMS state machine (no invalid transition reachable), join determinism (shuffled arrival order within watermark bounds → identical output)
- **Unit:** normalisers (including malformed → DLQ paths), feature functions, clock parsing
- **Integration:** replayer at `speed_multiplier=1.0` through the full live stack
- **Chaos:** kill Flink TaskManager mid-replay → verify checkpoint recovery + no duplicate rows in ClickHouse (validates D3)

---

## Failure Modes

| Failure | Impact | Mitigation |
|---------|--------|-----------|
| Flink TaskManager dies | Reprocessing from last checkpoint → duplicate inserts | Dedup token on ClickHouse batches (D3); consumer-side dedup on enriched.trades |
| Kafka broker down | Producers block, consumer lag grows | Single-broker dev; lag alerting; replay is idempotent |
| Redis down | Cache loss only | Circuit-break, rebuild from SQLite (D6) |
| ClickHouse down | Inserts buffer in Flink sink | Backpressure; job pauses rather than drops |
| Kalshi REST timeout | Unknown order state | Durable INTENT + client_order_id + reconciliation (§6) |
| WS sequence gap | Book state corrupt | Gap detection → invalidate → block intents until snapshot resync (§7) |
| NBA CDN outage | No game state for trades | Enriched rows emitted with null game state + staleness flag — visible, not silent |
| Malformed bronze record | — | DLQ with raw bytes + error (D8) |
| Idle game_state partitions | Watermark stall | `withIdleness(60s)` (§3) |

---

## Observability

Needed to debug Phase 1, not deferred to later:

- **Flink Web UI** — watermarks per operator, checkpoint stats, backpressure (built-in)
- **Late-data + DLQ counters** — Flink metrics, logged per minute
- **Consumer lag** — `kafka-consumer-groups --describe`
- **Information delay stats** — aggregate `r_info_delay_ms` from ClickHouse after each backfill
- Full Prometheus/Grafana deferred to Phase 4

**Latency budget (live mode, p99 targets):**

| Stage | Target |
|-------|--------|
| WS → Kafka producer | < 5ms |
| Kafka → Flink consumer | < 10ms |
| Enrichment (join + features) | < 50ms |
| Sink (Kafka + ClickHouse) | < 5ms |
| Strategy signal generation | < 1ms |
| OMS → Kalshi REST | < 500ms |
| **Total tick-to-order** | **< 600ms** |

Instrumented via `perf_counter_ns()` + HDR histogram from Phase 3 onward.

---

## Build Order

```
Phase 0 — Foundations
  0.1  Design doc complete
  0.2  Verify Kalshi client_order_id support — DONE (rejected-duplicate semantics)
  0.3  Golden game fixture: freeze 0042500121 bronze, hand-verify anchor facts

Phase 1 — Data pipeline
  1.1  Normalisation layer + DLQ paths (unit-tested, Hypothesis)
  1.2  Replayer: merge-sort, keyed partitioning, ordering verification
  1.3  Flink job: dual-view as-of join, asymmetric watermarks,
       idleness handling — golden game test passing
  1.4  ClickHouse schema + batched idempotent sink — chaos-kill test passing

Phase 2 — Research infrastructure
  2.1  Full 59-game backfill; validate vs settlement results
  2.2  Information delay analysis (receipt vs event view comparison)
  2.3  Ray backtest engine + immutable results store
  2.4  First strategy backtest (analytical model, KXNBAGAME)

Phase 3 — Live stack
  3.1  OMS: state machine + SQLite log + startup reconciliation
  3.2  Risk engine: limits, circuit breaker, sequence-gap guard
  3.3  Strategy service: Kafka consumer + control-plane gRPC
  3.4  Integration: replayer at 1x speed through full live stack,
       golden game output identical to batch replay

Phase 4 — Hardening
  4.1  Full latency instrumentation (HDR histograms)
  4.2  Prometheus + Grafana
  4.3  Chaos suite: TaskManager kill, Redis kill, REST timeout injection
```

---

## Deployment

Everything runs as containers on **one machine** — laptop for development, a single EC2 instance for full replays and live operation:

```
EC2 t3.xlarge (4 vCPU, 16 GB) — started on demand, stopped after
└── Docker Compose
    ├── kafka              (single broker, KRaft mode)
    ├── flink-jobmanager   (web UI :8081)
    ├── flink-taskmanager  ×2
    └── clickhouse         (:8123 / :9000)
```

Containers share the instance and communicate over the compose network by service name. The distributed-systems patterns (partitioning, checkpointing, keyed state) are identical to a multi-node deployment.

**Cost (development):** ~$0.17/hr on-demand t3.xlarge, ~$5–15/mo at a few hrs/week + EBS ~$4/mo + S3 ~$2/mo ≈ **$10–20/mo**. All software is open source. Live-season 24/7 would be ~$120/mo.

**Language:** PyFlink (DataStream API). Keeps the entire project in Python. Supports `KeyedCoProcessFunction`, broadcast state, event-time timers. PyFlink requires Python ≤3.12.

**Production path (not built):** Kafka → AWS MSK (~$50–150/mo). Flink → EKS. ClickHouse → ClickHouse Cloud or dedicated EC2. Migration requires no topology changes.
