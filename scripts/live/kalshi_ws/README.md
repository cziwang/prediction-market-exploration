# `scripts/live/kalshi_ws/`

Live Kalshi WebSocket ingester. Connects to Kalshi's authenticated WS,
subscribes to `orderbook_delta` for every currently-open KXNBAGAME
market, and archives each raw frame to
`s3://prediction-markets-data/bronze/kalshi_ws/` via `BronzeWriter`.
No transform, no silver — bronze only. v1 scope is deliberately narrow
(one channel, one series) per `docs/data-flow.md` open question #5.

Entry point: `python -m scripts.live.kalshi_ws` (runs `__main__.py`).
Related docs:

- [`docs/data-flow.md`](../../../docs/data-flow.md) — why this shape
- [`docs/live-kalshi-ws-service.md`](../../../docs/live-kalshi-ws-service.md) — systemd deployment + credential setup
- [`docs/ec2-bootstrap.md`](../../../docs/ec2-bootstrap.md) — one-time EC2 setup

## Architecture

Event-driven stream rather than a poller. Each connect attempt:

```
1. REST: list all open KXNBAGAME markets                   → N tickers
2. Sign WS handshake (RSA-PSS on timestamp+GET+path)       → handshake headers
3. Open WS to wss://api.elections.kalshi.com/trade-api/ws/v2
4. Send {"cmd":"subscribe", "channels":["orderbook_delta"],
         "market_tickers":[...all N tickers...]}
5. async for each inbound frame:
        emit to bronze with channel = frame["type"]
```

On disconnect: exponential backoff (1 s → 60 s), then reconnect. Every
reconnect re-runs step 1 (so newly-opened markets get picked up) and
produces a fresh `orderbook_snapshot` per market from Kalshi.

On empty market list (off-season, deep off-hours): idle 300 s, then
re-check.

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

Not a poll — Kalshi pushes. Observed frame volumes:

| Context                           | Observed delta rate                            |
|-----------------------------------|------------------------------------------------|
| Off-hours, no live NBA game       | ~40 `orderbook_delta` msgs/sec across all open markets |
| During a live NBA game            | expected 5–10× higher (not yet measured)       |

Snapshots (`orderbook_snapshot`) arrive exactly once per market per
subscribe — so after a fresh connect you see one snapshot per
subscribed market before deltas resume. Control messages
(`subscribed`, `error`) are rare and tiny.

Reconnect backoff uses module constants:

| Constant              | Value  | When it fires                               |
|-----------------------|--------|---------------------------------------------|
| `RECONNECT_INITIAL`   | 1 s    | First retry after a session failure         |
| `RECONNECT_MAX`       | 60 s   | Ceiling on exponential backoff              |
| `NO_MARKETS_BACKOFF`  | 300 s  | Retry cadence when KXNBAGAME has 0 open markets |

## What's written to S3

Bronze channel is keyed by the server-provided message `type`, so the
snapshot / delta split falls out naturally and control messages land
under their own tiny prefixes:

```
bronze/kalshi_ws/orderbook_snapshot/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/kalshi_ws/orderbook_delta/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/kalshi_ws/subscribed/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/kalshi_ws/error/YYYY/MM/DD/HH/{uuid}.jsonl.gz   # if Kalshi sends any
```

Flush triggers are the same as `nba_cdn/` (5 MB, 60 s, or shutdown).

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
- **Single WS connection.** All subscribed markets share one
  connection. If Kalshi ever enforces a per-connection subscription
  cap below the number of open KXNBAGAME markets (~50–60), we'd need
  to shard.
- **Startup-only market discovery.** New KXNBAGAME markets added
  mid-session are not picked up until the next reconnect. For
  game-day operations this means a restart (or natural reconnect)
  around new-market creation times.
