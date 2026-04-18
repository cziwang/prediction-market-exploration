# Data Flow: Ingestion → In-Process Fan-Out → Storage / Strategy

## Goal

Capture raw NBA play-by-play and raw Kalshi live market data, archive them byte-for-byte in S3 for backtesting and auditability, and — in the same process — feed a transformer that drives both the live trading strategy *and* a Parquet-based silver layer for notebook exploration.

The guiding principle: **one transform execution site.** Raw bytes are the wire format. The strategy never touches raw JSON. Backtest and live trading use the same bytes, the same transform code, and — because the transform is applied exactly once per event — produce bit-for-bit consistent outputs. Parity is structural, not disciplinary.

## Why this matters

The single largest source of "worked in sim, didn't work live" bugs is letting the live system and the backtest compute features differently. If the live path does:

```python
scoring_team = action["scoreHome"]   # in the strategy file
```

…and the backtest notebook does:

```python
df["home_score"] = df["action"].apply(lambda a: a.get("scoreHome"))   # in a pandas cell
```

…then one `None` handling or type coercion difference silently skews offline results. This design eliminates that risk structurally: the transform runs once, in the live process. Its output is simultaneously the strategy's input *and* the row written to silver. They cannot diverge because they are the same values.

## Design principle

**One live process per source.** It owns the wire connection (WebSocket or polling loop), fans each raw frame out to three destinations, and exits cleanly on signal. There is no external event log — the authoritative history is bronze S3.

```
┌─────────────────────────────────────────────────────────────┐
│  Live process (one per source)                              │
│                                                             │
│    ingester (WS / REST poll)                                │
│         │                                                   │
│         │  raw frame                                        │
│         ├──────────────▶  BronzeWriter  (async task)        │
│         │                       │  batch → gzip → S3        │
│         │                       ▼                           │
│         │                 s3://…/bronze/                    │
│         │                                                   │
│         ▼                                                   │
│    transform()                                              │
│         │                                                   │
│         │  typed event                                      │
│         ├──────────────▶  strategy.on_event()               │
│         │                 (in-process, sub-ms)              │
│         │                                                   │
│         └──────────────▶  SilverWriter  (async task)        │
│                                 │  pyarrow → Parquet → S3   │
│                                 ▼                           │
│                           s3://…/silver/                    │
└─────────────────────────────────────────────────────────────┘
```

- **Ingester**: talks to the external API/WebSocket, timestamps every record, and hands the raw bytes to the bronze writer and to `transform()`. Never interprets fields.
- **BronzeWriter**: async task, owns an in-memory buffer per `(source, channel)`. Flushes on size (5 MB) or time (60 s) by gzipping the batch and writing to S3 with a time-partitioned key. Flushes on clean shutdown.
- **`transform()`**: pure (NBA) or small-state (Kalshi orderbook) function mapping raw → typed event. Called synchronously on the ingester's task. Returns `None` for frames that don't produce an event.
- **Strategy**: `on_event(event)` is called synchronously in the same task. Must be non-blocking. All stateful strategy logic lives here.
- **SilverWriter**: async task, buffers typed events per event-type, flushes to Parquet on S3 periodically (e.g., every 5 min) and on shutdown.

## Why not Kinesis / Firehose / Glue

An earlier draft routed everything through a Kinesis Data Stream and wrote bronze with Firehose. That buys durable replay outside the process, independent multi-consumer fan-out, and managed batching to S3. For a solo exploration project with one consumer, the tradeoff isn't worth the operational surface area. The in-process design gives up the following; each has an accepted mitigation:

| Dropped capability | Mitigation |
|---|---|
| 7-day stream replay | Bronze on S3 is the replay source. Slightly slower (download + gunzip) but same bytes. |
| Durability before any consumer reads | Crash blast radius is the in-memory bronze buffer (≤ 5 MB / 60 s). Kalshi re-sends a fresh orderbook snapshot on reconnect; NBA poller resumes from the last committed state. |
| Independent multi-consumer fan-out | Only one consumer is planned (strategy + silver, both in-process). Adding one later means adding an async task, not new infra. |
| Managed back-pressure | Buffer size is capped; if S3 uploads stall for minutes, the process logs and exits (fail-fast beats silent memory growth). |

If any of these mitigations stops holding — e.g., a second independent consumer becomes real, or the strategy needs restart-independent bronze writes — revisit Kinesis.

## Proposed layout

```
app/
├── clients/                    # raw I/O, per-source
│   ├── nba_cdn.py                # NBA CDN REST (existing)
│   ├── kalshi_sdk.py             # Kalshi REST (existing)
│   └── kalshi_ws.py              # NEW — Kalshi WebSocket subscriber
├── services/
│   ├── s3_raw.py                 # existing — one-shot JSON puts for batch fetchers
│   ├── bronze_writer.py          # NEW — async batched gzip-JSONL writer
│   └── silver_writer.py          # NEW — async batched Parquet writer
├── transforms/                 # pure raw → typed
│   ├── __init__.py
│   ├── nba_pbp.py                # NBA action dict → ScoreEvent | PeriodChange | …
│   └── kalshi_ws.py              # WS frame → OrderBookUpdate | TradeEvent | …
├── events.py                   # frozen dataclasses for every domain event
└── strategy/                   # consumes events.py; never touches raw JSON
    └── …

scripts/
├── live/                         # NEW — live ingester+transformer+writer processes
│   ├── kalshi_ws.py                # one process: Kalshi WS → bronze + strategy + silver
│   └── nba_cdn.py                  # one process: NBA polling → bronze + strategy + silver
└── materialize/                  # rebuild-only, run manually after transform changes
    ├── nba_pbp.py
    └── kalshi_ws.py
```

One live process per source, so a Kalshi WS reconnect storm doesn't force an NBA poller restart and vice versa. They share `BronzeWriter`, `SilverWriter`, and strategy state wiring from `app/`.

## Ingestion layer

Each live process is structured the same way: async main loop, three destinations per frame, clean shutdown on signal.

### Kalshi WS (example)

```python
# rough shape of scripts/live/kalshi_ws.py
async def main():
    strategy = Strategy.bootstrap_from_silver()
    bronze = BronzeWriter(source="kalshi_ws")
    silver = SilverWriter(source="kalshi_ws")
    orderbook_state = defaultdict(OrderBookState)

    async with bronze, silver:                        # flush on exit
        async for frame in kalshi_ws.stream():
            t_receipt = time.time()
            raw_bytes = wrap_raw(frame, t_receipt)    # adds source/channel/seq/t_receipt

            await bronze.emit(raw_bytes, channel=frame["channel"])

            event = apply_transform(frame, t_receipt, orderbook_state)
            if event is None:
                continue
            strategy.on_event(event)
            await silver.emit(event)
```

Key properties:
- **`bronze.emit` is fire-and-forget** (pushes onto an `asyncio.Queue`). The WS reader never waits on S3.
- **`transform` + `strategy.on_event` run inline.** They must be cheap. Heavy strategy computation must be offloaded (`asyncio.to_thread` or similar), never blocking the WS read.
- **`silver.emit` is fire-and-forget** (in-memory buffer; flusher task owns S3 writes).
- **`async with` ensures flush on shutdown.** SIGINT → both writers drain their buffers to S3 before the process exits.

### NBA CDN polling

Same shape, different ingester: `async for frame in nba_cdn.poll_live()` where the client yields one dict per poll tick per game. Transform is stateless (`parse_action`). Same bronze and silver writers, with `source="nba_cdn"`.

The existing `scripts/nba_cdn/poll_live.py` (per-game local JSONL → gzip upload on `gameStatus=3`) is transitional. Migration to this design replaces it with `scripts/live/nba_cdn.py` and changes the bronze layout; notebooks that depend on the old layout need updating. Defer until the Kalshi path is working end-to-end.

## Bronze storage

Authoritative raw history, written by `BronzeWriter` directly from the live process. Gzipped JSONL, one file per flush, time-partitioned keys.

```
s3://prediction-markets-data/bronze/
  kalshi_ws/
    orderbook_delta/2026/04/18/22/<uuid>.jsonl.gz
    trade/2026/04/18/22/<uuid>.jsonl.gz
    lifecycle/2026/04/18/22/<uuid>.jsonl.gz
  nba_cdn/
    live_pbp/2026/04/18/22/<uuid>.jsonl.gz
```

Each JSONL line is a wrapped record:

```json
{
  "source": "kalshi_ws",
  "channel": "orderbook_delta",
  "market_ticker": "KXNBAGAME-25APR18LALBOS-LAL",
  "seq": 4821,
  "t_receipt": 1713456789.456,
  "frame": { /* raw Kalshi WS message, untouched */ }
}
```

`BronzeWriter` buffers per `(source, channel)` so each prefix gets contiguous batches. Flush triggers: 5 MB uncompressed, 60 s elapsed, or shutdown.

## Silver writes

`SilverWriter` buffers typed events in memory grouped by event type, periodically writes a Parquet file per type using `pyarrow`, and uploads to S3. Flush triggers: 5 min elapsed, N rows, or shutdown.

```python
# rough shape of app/services/silver_writer.py
class SilverWriter:
    def __init__(self, source: str, flush_seconds: int = 300):
        self._buffers: dict[str, list[Event]] = defaultdict(list)
        ...

    async def emit(self, event: Event) -> None:
        self._buffers[type(event).__name__].append(event)

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(self._flush_seconds)
            await self._flush()

    async def _flush(self):
        for event_type, rows in self._buffers.items():
            table = pa.Table.from_pylist([asdict(r) for r in rows])
            buf = pa.BufferOutputStream()
            pq.write_table(table, buf, compression="zstd")
            key = f"silver/{self._source}/{event_type}/date={today}/v={VERSION}/part-{uuid}.parquet"
            s3.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue().to_pybytes())
        self._buffers.clear()
```

This is `pyarrow` writing directly from the transformer — option 2 from the earlier draft, which is now the default. The Firehose + Glue variant was dropped along with Kinesis; revisit if the event schemas stabilize and the per-flush pyarrow code becomes a hotspot.

### Why Parquet

- **Columnar**: reading `t_receipt, bid_yes, ask_yes` touches ~3 columns' bytes, not all.
- **Schema embedded**: every column has a declared type. No `.astype(int)` dance in notebooks.
- **Compressed** (snappy/zstd). Typically 5–10× smaller than gzipped JSONL.
- **Every library reads it natively**: `pd.read_parquet`, `pl.read_parquet`, `duckdb.query("SELECT … FROM 's3://…/*.parquet'")`.
- **No server to run.** S3 is the database. Add Athena on top when ad-hoc SQL across months of data becomes routine; the same files serve both.

### S3 layout

```
silver/
  nba_cdn/
    ScoreEvent/date=2026-04-18/v=1/part-*.parquet
    PeriodChange/date=2026-04-18/v=1/part-*.parquet
  kalshi_ws/
    OrderBookUpdate/date=2026-04-18/v=1/part-*.parquet
    TradeEvent/date=2026-04-18/v=1/part-*.parquet
```

**One prefix per event type**, Hive-style `date=YYYY-MM-DD/v=N/` partitioning. The `v=N/` segment pins the transform version: when the event dataclass changes incompatibly, bump the version, regenerate, and notebooks pin to a version.

### Strategy bootstrap at startup

The strategy is stateful over the event stream (rolling stats, score state, orderbook state). On process start, it needs history, not just live events.

```
startup:
  1. Query silver Parquet for the last N hours of events.
  2. Feed them through strategy.on_event() as bootstrap.
  3. Open the live connection; continue.
```

Silver is the bootstrap source because it's already transformed — no need to re-read bronze or re-apply transforms. There's no stream cursor to resume from; any events dropped during restart downtime are lost *from the strategy's view*, but bronze captured them and the next rebuild picks them up.

## The event vocabulary

Every downstream component (live strategy, backtest replay, monitoring) sees the **same** set of typed events on one timeline, sorted by `t_receipt`. Sketch:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ScoreEvent:
    t_receipt: float        # our wall clock when we first observed the action
    game_id: str
    action_number: int      # NBA's monotonic sequence; idempotency key
    period: int
    clock: str              # raw NBA clock string, e.g. "PT04M22.00S"
    home: int
    away: int
    scoring_team: str | None   # "home" | "away" | None for non-scoring actions
    points: int                # 0, 1, 2, or 3

@dataclass(frozen=True)
class PeriodChange:
    t_receipt: float
    game_id: str
    new_period: int

@dataclass(frozen=True)
class OrderBookUpdate:
    t_receipt: float
    market_ticker: str
    bid_yes: int            # cents
    ask_yes: int            # cents
    bid_size: int
    ask_size: int

@dataclass(frozen=True)
class TradeEvent:
    t_receipt: float
    market_ticker: str
    side: str               # "yes" | "no"
    price: int              # cents
    size: int

Event = ScoreEvent | PeriodChange | OrderBookUpdate | TradeEvent
```

Design choices worth naming:

- **`frozen=True`**: events are immutable value objects — safe to pass around, log, and hash.
- **Integer cents, not floats**: Kalshi trades in integer cents. Never use `float` for money.
- **`t_receipt` on every event**: the strategy's ordering key and staleness check.
- **Narrow shape**: each event is a flat record, not a nested object. Makes pandas / DuckDB queries trivial.
- **Open-ended union**: we add new event types as the strategy needs them; `Event` grows.

## Rebuild (`scripts/materialize/`)

When the transform code changes, last month's silver is now out-of-date relative to the current version. Run the rebuild script:

```bash
python -m scripts.materialize.kalshi_ws --from 2026-03-01 --to 2026-04-01 --version 2
```

It reads bronze from S3, runs the current transform, and writes to `silver/.../v=2/`. Old `v=1/` stays in place until you delete it. Because bronze is authoritative, silver can always be regenerated exactly.

## What stays raw (bronze)

Bronze S3 files always hold the **raw** bytes from NBA and Kalshi — never derived events. Two reasons:

1. **Schema evolution without backfills.** If six months from now the strategy needs `action["assistPersonId"]` or a Kalshi field we didn't extract, we re-run the transform over bronze. If we'd stored only derived `ScoreEvent`, we'd have to refetch or accept a blind spot.
2. **Transform bug recovery.** When (not if) we find a bug in `parse_action`, we fix the function, rebuild silver, and move on. No data loss.

Bronze is the permanent home. Silver is a cache of the transformer's output; it can always be rebuilt from bronze.

## Backtest ↔ live parity

Parity is structural:

| Path | Inputs | Transform | Consumer |
|---|---|---|---|
| Live | WS/REST frames | `app.transforms.*` inline in live process | `strategy.on_event()` (in-process) |
| Fast backtest | Silver Parquet from S3 | (already applied at write time) | Notebook or `strategy.on_event()` replayed |
| Cold backtest / rebuild | Bronze JSONL from S3 | `app.transforms.*` at rebuild time | Notebook or `strategy.on_event()` replayed |

The live path and silver are produced by the *same function call* in the live process. They are identical by construction. The cold backtest re-runs transforms — useful when the transform has been updated and you want to regenerate silver.

## Anti-patterns

- **Transforming in the ingester client.** `app/clients/kalshi_ws.py` yields raw frames and nothing else. Interpretation happens in the live process entry point, once.
- **Transforming in the strategy.** Pulling `raw["scoreHome"]` out of a strategy function is a red flag. If the strategy needs a field, extend the event dataclass and the transform.
- **Writing bronze or silver from a second place.** BronzeWriter and SilverWriter are the only writers. No ad-hoc `s3.put_object` calls for live data.
- **Blocking the ingester loop.** `strategy.on_event` and `transform` run on the WS reader's task. If they ever need to block on I/O or heavy CPU, offload to a thread — don't stall the read.
- **Treating silver as authoritative.** Silver is a cache of the transform's output. If it disagrees with bronze-through-the-current-transform, rebuild. Never hand-patch Parquet.
- **Sharing writer state across processes.** Each live process owns its own `BronzeWriter` / `SilverWriter`. Don't try to have two processes write to the same prefix concurrently — rely on process-per-source isolation and distinct filenames (uuid) per flush.

## Testing

Pure library functions are easy to test. One golden-file pattern works well:

```
tests/
└── transforms/
    ├── fixtures/
    │   ├── nba_action_made_three.json
    │   ├── nba_action_turnover.json
    │   ├── kalshi_ws_orderbook_snapshot.json
    │   └── kalshi_ws_orderbook_delta.json
    └── test_nba_pbp.py
```

Each fixture is one raw record captured from a real response. The test calls `parse_action(fixture, t_receipt=0)` (or `OrderBookState.apply(...)`) and asserts the resulting typed event. Low effort, catches schema drift early.

Additionally, a simple property test that `app.transforms` is pure:

```python
def test_transforms_have_no_clock_or_env_access():
    # Inspect the module; fail if any transform calls time.time(), os.getenv, etc.
    ...
```

Catches the non-determinism class of bugs that would break backtest parity.

## Open questions

1. **Silver flush cadence.** 5 min feels right for notebook freshness; too short creates lots of tiny Parquet files (bad for Athena later). Compaction can come later.
2. **Bronze file sizing.** 5 MB / 60 s gives ~hundreds of KB gzipped — fine for ad-hoc re-reads, smallish for bulk scans. Revisit if bronze replay becomes a hotspot.
3. **Process supervision.** For now, run under `systemd` / `tmux` / a laptop terminal. If uptime matters, introduce a supervisor or run on ECS/Fargate.
4. **NBA migration.** Current NBA poller writes local JSONL → S3 on game end. Migrating it to `scripts/live/nba_cdn.py` + `BronzeWriter` changes the bronze layout notebooks depend on. Defer until after Kalshi WS is live.
5. **Subscription scope for Kalshi.** v1 covers `KXNBAGAME` only. `KXNBASPREAD`/`KXNBATOTAL`/player props expand after first validated live week.
6. **Retroactive NBA corrections.** The poller detects them via periodic snapshot hashes but doesn't apply them. Does the transform re-emit corrected `ScoreEvent` with the same `action_number` but a new `t_receipt`? Consumers would treat `(game_id, action_number)` as an upsert key. Defer until observed.
7. **Kalshi WS gap handling.** On a `seq` gap, resubscribe and consume the fresh snapshot. Transform resets `OrderBookState`, seeds from the snapshot. Standard pattern but worth implementing and testing with synthetic gaps.
8. **Strategy checkpointing.** Bootstrap from silver on start, then resume from live. Events dropped during downtime are visible in bronze but not to the running strategy — acceptable for exploration; revisit if live trading goes real.
9. **Schema versioning.** Additive-only changes to event dataclasses for now. Breaking changes go in a new `v=N/` silver partition. Revisit if a schema change can't be additive.
10. **When to reintroduce Kinesis.** Triggers: a second independent consumer becomes real, the strategy must survive ingester restarts without dropping live events, or bronze replay latency (download + gunzip) becomes painful for development.
