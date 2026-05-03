# `scripts/live/kalshi_ws/`

Live Kalshi WebSocket ingester. Connects to Kalshi's authenticated WS,
subscribes to `orderbook_delta` and `trade` for every currently-open
market across the four NBA series `KXNBAGAME`, `KXNBASPREAD`,
`KXNBATOTAL`, and `KXNBAPTS`, and archives each raw frame to
`s3://prediction-markets-data/bronze/kalshi_ws/` via `BronzeWriter`.
When `MM_ENABLED=1`, frames also flow through `KalshiTransform` →
`MMStrategy` (paper trading) → `SilverWriter` (typed events to
Parquet). See [`docs/deploy-mm-paper.md`](../../../docs/deploy-mm-paper.md)
for strategy activation and monitoring.

Entry point: `python -m scripts.live.kalshi_ws` (runs `__main__.py`).
Related docs:

- [`docs/data-flow.md`](../../../docs/data-flow.md) — why this shape
- [`docs/live-kalshi-ws-service.md`](../../../docs/live-kalshi-ws-service.md) — systemd deployment + credential setup
- [`docs/ec2-bootstrap.md`](../../../docs/ec2-bootstrap.md) — one-time EC2 setup

## Kalshi concepts

Two terms from Kalshi's API show up throughout this ingester: **series**
and **channel**. They are independent axes — you pick a channel (the
kind of updates you want) and a set of markets (often filtered by
series) to subscribe to.

### Series

A **series** is a group of related markets sharing a ticker prefix. All
NBA win/loss markets live under `KXNBAGAME`; all NBA point-spread
markets live under `KXNBASPREAD`. A specific NBA game (e.g. Lakers @
Celtics on 2026-04-18) is one *event*, and each series spawns one or
more *markets* under that event:

| Series        | Markets per game | Example ticker                         | What it trades                  |
|---------------|------------------|----------------------------------------|---------------------------------|
| `KXNBAGAME`   | 2 (one per team) | `KXNBAGAME-25APR18LALBOS-LAL`          | Will the Lakers win?            |
| `KXNBASPREAD` | many             | `KXNBASPREAD-25APR18LALBOS-B3.5`       | Will BOS cover −3.5?            |
| `KXNBATOTAL`  | many             | `KXNBATOTAL-25APR18LALBOS-T220.5`      | Will combined score be ≥ 220.5? |
| `KXNBAPTS`    | many             | `KXNBAPTS-25APR18LALBOS-LJAMES-P30.5`  | Will LeBron score ≥ 30.5?       |

Ticker anatomy: `<SERIES>-<EVENT_CODE>-<OUTCOME>` where `EVENT_CODE`
encodes date + team-matchup (`25APR18LALBOS` = Apr 18 2025, LAL @ BOS)
and `OUTCOME` identifies the specific market within that event
(`LAL` = Lakers-win side; `B3.5` = Boston spread −3.5; etc.). Exact
encoding varies by series.

Full NBA series catalogue (from `CLAUDE.md`): `KXNBAGAME`,
`KXNBASPREAD`, `KXNBATOTAL`, `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`,
`KXNBA3PT`, `KXNBABLK`, `KXNBASTL`, `KXNBA` (Finals winner),
`KXNBASERIES` (playoff series), `KXNBAPLAYOFF`, `KXNBAALLSTAR`.

**Current scope: `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`, `KXNBAPTS`.**
Game win/loss, point spread, total over/under, and player-points
markets. The other NBA series (rebounds, assists, threes, etc.) are a
one-line edit to `SERIES_TICKERS` in `__main__.py` plus a service
restart.

### Channel

A **channel** is a named topic on Kalshi's WebSocket that carries a
specific *kind* of update about whatever markets you've filtered to.
Different channels carry different payloads about the same market.

Public channels Kalshi exposes:

| Channel                | Payload                                                          | When to use                                                      |
|------------------------|------------------------------------------------------------------|------------------------------------------------------------------|
| `orderbook_delta`      | `orderbook_snapshot` once + incremental deltas of bid/ask sizes  | Reconstruct the full order book. **What this ingester uses.**    |
| `ticker`               | Top-of-book (best bid, best ask) + last trade + volume snapshot  | Lightweight price feed when you don't need book depth            |
| `trade`                | One message per executed public trade                            | Build the trade tape / volume profile                            |
| `market_lifecycle_v2`  | State transitions: opened, closed, determined, settled           | Know when a market stops accepting orders                        |
| `event_lifecycle`      | Event-level meta (new event announced, strike date updated, …)   | Track the market calendar                                        |

**Subscribe shape.** A subscribe command always names one or more
channels plus a market filter. Channel and series are independent — a
single subscription can pick any channel for any set of tickers from
any series:

```json
{"id": 1, "cmd": "subscribe",
 "params": {"channels": ["orderbook_delta", "trade"],
            "market_tickers": ["KXNBAGAME-25APR18LALBOS-LAL",
                               "KXNBAGAME-25APR18LALBOS-BOS"]}}
```

That one subscription would deliver **two** channels' worth of updates
for **two** markets. The current ingester uses only `orderbook_delta`
and passes the full list of open KXNBAGAME tickers at connect time.

**Series filter on subscribe?** No such thing — Kalshi's subscribe API
accepts `market_ticker` / `market_tickers` / `market_id` / `market_ids`
as filters, but not `series_ticker`. That's why the ingester first
queries REST for open KXNBAGAME tickers and passes them explicitly.

## Architecture

Event-driven stream rather than a poller. Each connect attempt:

```
1. REST (per series in SERIES_TICKERS):
      list all open markets → {KXNBAGAME: [...], KXNBASPREAD: [...], ...}
2. Sign WS handshake (RSA-PSS on timestamp+GET+path) → handshake headers
3. Open WS to wss://api.elections.kalshi.com/trade-api/ws/v2
4. For each non-empty series, send one subscribe:
      {"id": N, "cmd":"subscribe",
       "params":{"channels":["orderbook_delta","trade"],
                 "market_tickers":[...tickers for that series...]}}
5. async for each inbound frame:
      emit to bronze with channel = frame["type"]
```

One subscribe per series (not per channel) because Kalshi's subscribe
takes `market_tickers` but not `series_ticker` — it's cleaner to let
the server assign a distinct `sid` per series so a future gap-handling
path can resubscribe just the affected series.

On disconnect: exponential backoff (1 s → 60 s), then reconnect. Every
reconnect re-runs step 1 (newly-opened markets get picked up) and
produces a fresh `orderbook_snapshot` per market from Kalshi.

On empty market list (every series has 0 open markets — off-season,
deep off-hours): idle 300 s, then re-check.

Kalshi requires an authenticated connection even for public market
channels. `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` must be
set in `.env` — see
[`docs/live-kalshi-ws-service.md`](../../../docs/live-kalshi-ws-service.md)
for key-creation and file-placement details.

## What's parallelised

Even less than `nba_cdn/`. One WS connection, one `async for` loop
consuming frames serially, one `BronzeWriter` flushing in the
background. The async model still buys:

1. **Background bronze flushes** — same as `nba_cdn/`.
2. **Non-blocking S3 writes** — same as `nba_cdn/`.
3. **Fire-and-forget `emit()`** — frames land in memory and the WS
   reader never waits on S3.

What's **not** parallelised: subscription management is serial, and
there's no multiplexing across multiple WS connections. For v1's
single-series scope that's fine; if we fan out to many series later
we might want a connection per series-group.

## Stream cadence

Not a poll — Kalshi pushes. Observed frame volumes from a ~23 s
smoke test at 494 subscribed markets across the four NBA series
(KXNBAGAME=56, KXNBASPREAD=73, KXNBATOTAL=77, KXNBAPTS=288):

| Frame type            | Count in 23 s | Rate            | Notes                                              |
|-----------------------|---------------|-----------------|----------------------------------------------------|
| `orderbook_snapshot`  | 494           | one-time burst  | One per market, right after each subscribe         |
| `orderbook_delta`     | 5,355         | ~233/s          | Scales roughly with subscribed market count        |
| `trade`               | 139           | ~6/s            | Only fires on executed trades; goes up during games|
| `subscribed` / `ok`   | ~10           | once per connect| Control acks, tiny                                 |

During a live NBA game, expect `orderbook_delta` and `trade` rates
to climb significantly — not yet measured. Snapshots only reappear on
reconnect (or if a new market is added and the process next
reconnects).

Reconnect backoff uses module constants:

| Constant              | Value  | When it fires                               |
|-----------------------|--------|---------------------------------------------|
| `RECONNECT_INITIAL`   | 1 s    | First retry after a session failure         |
| `RECONNECT_MAX`       | 60 s   | Ceiling on exponential backoff              |
| `NO_MARKETS_BACKOFF`  | 300 s  | Retry cadence when **every** series has 0 open markets |

## Write cadence

Inbound frames are cheap — they land in `BronzeWriter`'s in-memory
buffer via `emit()`. Actual S3 writes only happen when a buffer
flushes. Buffers are **per-channel and independent**, with three
flush triggers (defaults from `app/services/bronze_writer.py`):

| Trigger          | Threshold         | In practice for kalshi_ws                                              |
|------------------|-------------------|------------------------------------------------------------------------|
| Time elapsed     | 60 s              | **Dominant trigger.** Every active channel flushes once per minute.    |
| Buffer size      | 5 MB uncompressed | Not observed at off-hours volume. May fire on `orderbook_delta` during live NBA games. |
| Process shutdown | SIGINT / SIGTERM  | One-shot — drains every non-empty buffer before the process exits.     |

What that means for PUT volume at the smoke-test scope (494 markets,
off-hours), per channel:

| Channel                | Writes / hour         | Typical gzipped size per write         |
|------------------------|-----------------------|-----------------------------------------|
| `orderbook_delta`      | ~60 (one per minute)  | ~100–400 KB                             |
| `trade`                | ~60 (one per minute)  | single-digit KB                         |
| `orderbook_snapshot`   | only on reconnect     | ~70 KB (one PUT for all 494 markets)    |
| `subscribed` / `unsubscribed` / `ok` | only on reconnect | <1 KB each                |

So during normal steady-state: **~2 S3 PUTs per minute** total (one
for `orderbook_delta`, one for `trade`), plus a handful of
control-message PUTs after each reconnect.

During a live NBA game, `orderbook_delta` traffic climbs and its
buffer may hit the 5 MB threshold before the 60 s timer — meaning
more frequent, similar-sized files rather than a single-large
dump-every-minute pattern. Still safe; just denser. If the file
count becomes a hotspot (Athena scans, S3 PUT cost), lower
`BronzeWriter`'s `flush_seconds` or raise `flush_bytes` — the same
writer is shared with `scripts/live/nba_cdn/`, so any tuning applies
to both ingesters.

Timing note: flushes are triggered by two asyncio paths — a
background `_flush_loop` task that wakes every `flush_seconds` and
calls `_flush_all()`, and inline size-check after every `emit()`.
Neither path blocks the WS reader: the actual `s3.put_object` call
is offloaded to a thread via `asyncio.to_thread`, so receiving and
buffering continues while bytes are going over the wire.

## What's written to S3

Bronze channel is keyed by the server-provided message `type`, so the
snapshot / delta split falls out naturally and control messages land
under their own tiny prefixes:

```
bronze/kalshi_ws/orderbook_snapshot/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/kalshi_ws/orderbook_delta/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/kalshi_ws/trade/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/kalshi_ws/subscribed/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/kalshi_ws/unsubscribed/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/kalshi_ws/ok/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/kalshi_ws/error/YYYY/MM/DD/HH/{uuid}.jsonl.gz   # if Kalshi sends any
```

## Record schema

```json
{
  "source":    "kalshi_ws",
  "channel":   "<type from Kalshi — orderbook_snapshot | orderbook_delta | ...>",
  "t_receipt": 1713456789.456,
  "frame":     { "type": "...", "sid": 42, "seq": 123, "msg": { ... } }
}
```

`frame` preserves the whole Kalshi envelope including `sid` and `seq`,
since both are load-bearing for replay and gap detection.

## Running it

Foreground (once creds are in `.env` and deps installed):

```bash
source .venv/bin/activate
python -m scripts.live.kalshi_ws
```

As a service on EC2: see
[`docs/live-kalshi-ws-service.md`](../../../docs/live-kalshi-ws-service.md).

## `conn_id`: scoping frames to a connection lifetime

Every frame archived to bronze includes a `conn_id` field — a random
UUID hex string generated once per WebSocket connection attempt (i.e.
once per call to `_connect_once()`). All frames from the same
connection share the same `conn_id`; a reconnect produces a new one.

### Why it exists

The problem it solves: **`sid` (subscription ID) is not globally
unique.** Kalshi assigns `sid` sequentially starting from 1 on each
connection. After a reconnect, the new connection also starts at
`sid=1`. Without `conn_id`, there is no way to tell which snapshot a
given delta belongs to — you might apply deltas from connection B to a
snapshot from connection A, corrupting the reconstructed book.

This matters for order book reconstruction:

1. A snapshot seeds the book state for a specific market.
2. Deltas update that state incrementally.
3. Both are only valid within the same connection lifetime.

If you mix deltas across connections, the reconstructed book drifts
from reality — prices no longer add up, levels go negative, and you
get **crossed books** (best bid > best ask), which are impossible on a
real exchange.

### How to use it downstream

When reconstructing a book from bronze data:

```python
# Group by conn_id to isolate each connection's frames
for conn_id, group in delta_df.groupby("conn_id"):
    # Find the snapshot for this ticker from the SAME conn_id
    snap = snap_df[
        (snap_df["conn_id"] == conn_id) &
        (snap_df["ticker"] == ticker)
    ]
    if snap.empty:
        continue  # no snapshot for this connection — can't seed
    # Seed from snapshot, then apply deltas in seq order
    ...
```

Without `conn_id` scoping, the order book notebook's reconstruction
showed 60% negative spreads — a direct result of applying deltas from
one connection to a snapshot from another. Scoping by `conn_id`
eliminates this class of error entirely.

### When `conn_id` is missing

Early bronze data (before the field was added) has `conn_id: null`.
For those files, fall back to time-window heuristics: scope deltas to
a window after the snapshot's `t_receipt`, and discard any
reconstruction that produces crossed books. This is lossy but better
than mixing connections blindly.

## Known limitations

- **Same 60 s crash window as `nba_cdn/`**, but partly mitigated:
  Kalshi re-sends a full `orderbook_snapshot` per market on every
  reconnect, so the book is self-healing even if deltas are lost
  during a crash.
- **Gap handling not implemented.** Kalshi's envelope has a
  monotonic `seq` per subscription; the current script archives
  frames verbatim regardless of `seq` gaps. If a gap occurs, bronze
  still contains every frame we actually received, but a future
  transform will need to handle the gap by triggering a re-snapshot
  (unsubscribe + resubscribe). Deferred per `data-flow.md` open
  question #7.
- **Single WS connection.** All ~500 subscribed markets across four
  series share one connection. Kalshi doesn't document a per-connection
  subscription cap, but if we fan out further (e.g. add rebounds,
  assists, threes) or Kalshi enforces a limit, we'd need to shard —
  natural split would be one connection per series.
- **Startup-only market discovery.** New markets added mid-session
  (e.g. tomorrow's games appearing on Kalshi mid-afternoon) are not
  picked up until the next reconnect. For game-day operations this
  means a restart (or natural reconnect) around new-market creation
  times.
