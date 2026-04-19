# `scripts/live/` — live ingesters

One long-running process per data source. Each process owns its wire
connection, a `BronzeWriter`, and (later) a transform + strategy +
silver writer. Shared design rationale lives in
[`docs/data-flow.md`](../../docs/data-flow.md); deep-dives for each
ingester live next to the code.

| Ingester | Source | Protocol | Code README | Deployment doc |
|---|---|---|---|---|
| `nba_cdn/` | `cdn.nba.com` | REST polling | [`nba_cdn/README.md`](nba_cdn/README.md) | [`live-nba-cdn-service.md`](../../docs/live-nba-cdn-service.md) |
| `kalshi_ws/` | `api.elections.kalshi.com` | authenticated WebSocket | [`kalshi_ws/README.md`](kalshi_ws/README.md) | [`live-kalshi-ws-service.md`](../../docs/live-kalshi-ws-service.md) |

Both run the same pattern: ingester loop → `bronze.emit()` →
`BronzeWriter` → gzipped JSONL on S3. No transform, no silver yet —
bronze only. A third ingester would drop in next to these without
changing anything they already do.

## Conventions shared across ingesters

- **Entry point is `python -m scripts.live.<name>`** — each ingester
  is a package with `__init__.py` + `__main__.py`. Systemd unit
  files reference that module path directly.
- **Source tag is the directory name.** `source="nba_cdn"`,
  `source="kalshi_ws"`. Appears on every bronze record and in the
  S3 prefix (`bronze/{source}/...`).
- **Bronze channel is the inner event kind.** For pollers this is
  the endpoint (`scoreboard`, `live_pbp`). For the WS ingester it's
  whatever the server puts in `frame["type"]`
  (`orderbook_snapshot`, `orderbook_delta`). One S3 prefix per
  channel makes per-event-type queries cheap.
- **`t_receipt` is the clock for backtest replay.** Every record
  stamps the wall-clock time the response finished parsing. Never
  use server timestamps for replay ordering — they can be skewed or
  revised.
- **Shutdown is `async with BronzeWriter` + SIGINT/SIGTERM.** The
  signal handler flips a shutdown flag and (for Kalshi) closes the
  WS; the writer's `__aexit__` drains every buffered channel to S3
  before the process exits.
- **No on-disk staging.** Crash window is one `BronzeWriter` flush
  interval (60 s) or buffer size (5 MB), whichever comes first.
