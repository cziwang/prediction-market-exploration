# `scripts/live/nba_cdn/`

Live NBA CDN ingester. Polls `cdn.nba.com` and archives every raw
frame to `s3://prediction-markets-data/bronze/nba_cdn/` via
`BronzeWriter`. No transform, no silver — bronze only.

Entry point: `python -m scripts.live.nba_cdn` (runs `__main__.py`).
Related docs:

- [`docs/data-flow.md`](../../../docs/data-flow.md) — why this shape, not why this file
- [`docs/live-nba-cdn-service.md`](../../../docs/live-nba-cdn-service.md) — systemd deployment
- [`docs/ec2-bootstrap.md`](../../../docs/ec2-bootstrap.md) — one-time EC2 setup

## Architecture

Single async event loop in `Ingester.run()`. Each tick fetches today's
scoreboard, then — for every game currently in `gameStatus=2` (live) —
polls that game's PBP and boxscore. Newly-finalised games get one
last poll and are dropped from tracking.

```
each tick:
    fetch scoreboard   → emit (always)
    fetch odds         → emit (if etag changed)
    for each game with gameStatus=2 (live):
        fetch PBP      → emit any new actions (by actionNumber)
        fetch boxscore → emit (if etag changed)
    for each game that just went gameStatus=3 (final):
        one last PBP + boxscore poll, then drop from tracking
    sleep(tick_interval)
```

Game discovery is implicit — the scoreboard is the coordinator. Any
`gameStatus=2` game the scoreboard reports gets added to
`Ingester.games`; anything that transitions to `gameStatus=3` is
finalised and removed. Restart starts from an empty game set and
rediscovers on the first tick (no on-disk state).

## What's parallelised

Not much within a tick — HTTP fetches inside `_tick()` are awaited
serially. The async model buys three things:

1. **Background bronze flushes.** `BronzeWriter._flush_loop` runs as
   a separate task, flushing buffers every 60 s independent of what
   the ingester is doing.
2. **Non-blocking S3 writes.** Each flush offloads `s3.put_object` to
   a thread (`asyncio.to_thread`), so the event loop keeps servicing
   ticks while bytes are going over the wire.
3. **Fire-and-forget `emit()`.** `bronze.emit()` just appends to an
   in-memory buffer — it never waits on S3. The tick loop is bounded
   by HTTP latency, not S3 latency.

What is **not** parallelised: PBP and boxscore fetches across live
games run in sequence. With ~10 concurrent games and ~50 ms RTT each,
a full tick is ~1 s, well under the 3 s interval — so the simplicity
wins. Revisit if tick time ever approaches the interval.

## Poll cadence

`_tick()` chooses the next sleep based on today's scoreboard:

| Scoreboard state                          | Tick interval | Source constant              |
|-------------------------------------------|---------------|------------------------------|
| Any game `gameStatus=2` (live)            | **3 s**       | `POLL_INTERVAL`              |
| Games scheduled or recently final, none live | **60 s**   | `SCOREBOARD_INTERVAL_ACTIVE` |
| Empty slate (off-day, off-season)         | **300 s**     | `SCOREBOARD_INTERVAL_IDLE`   |

Scoreboard + odds are fetched once per tick. PBP + boxscore are
fetched once per tick **per live game**. Etags on PBP / odds /
boxscore mean 304 responses are free — no emission, no buffer growth,
no S3 write downstream.

**Empty-slate short-circuit.** When the scoreboard reports no games
at all (off-season / off-day), the scoreboard response and the odds
poll are **not archived** — the tick still runs (the fetch is needed
to discover when games reappear), but no records are emitted. This
keeps off-season S3 clutter at zero. Any non-empty slate (scheduled,
live, or final games present) restores normal archival.

## Write cadence

Responses that produce records are emitted into `BronzeWriter`'s
in-memory buffers via `emit()`. Actual S3 writes happen only when a
buffer flushes. Buffers are **per-channel and independent**, with
three flush triggers (defaults from `app/services/bronze_writer.py`):

| Trigger          | Threshold         | In practice for nba_cdn                                              |
|------------------|-------------------|-----------------------------------------------------------------------|
| Time elapsed     | 60 s              | **Dominant trigger.** Every active channel flushes once per minute.  |
| Buffer size      | 5 MB uncompressed | Not observed at typical volume; boxscore might approach it on a busy night. |
| Process shutdown | SIGINT / SIGTERM  | One-shot — drains every non-empty buffer before the process exits.   |

Empty buffers are no-ops. A channel with zero queued records in its
60 s window produces **zero** S3 writes. That's why in deep idle
(off-season or an off-day) we only see the occasional scoreboard file
— PBP / boxscore produce nothing at all until a game is actually live,
and odds stays silent when `cdn.nba.com` returns 304 for hours.

Rough PUTs/hour by scoreboard state, per channel, during steady state:

| Scoreboard state                              | scoreboard | odds         | live_pbp           | boxscore           |
|-----------------------------------------------|-----------|--------------|--------------------|--------------------|
| Any game live (3 s tick)                      | ~60       | ≤ 60         | ~60 per active game window | ≤ 60 per active game |
| Scheduled or recently final only (60 s tick)  | ~60       | ≤ 60         | 0                  | 0                  |
| Empty slate (300 s tick)                      | **0**     | **0**        | 0                  | 0                  |

"≤" on `odds` and `boxscore` because they're etag-deduped — if the
upstream response is unchanged, the channel's buffer gets no record
and the 60 s flush is a no-op. `live_pbp` emits one record per **new**
action seen, not per tick, so its PUT count depends on how many new
actions NBA adds per minute — typically a handful during active play,
zero in timeouts and between periods.

Empty-slate rows are zero by design: the tick short-circuits emission
when the scoreboard reports no scheduled / live / final games (see
"Empty-slate short-circuit" under Poll cadence). The HTTP call still
happens — we need it to detect when games come back — but nothing
lands in bronze.

Timing note: flushes are triggered by two asyncio paths — a
background `_flush_loop` task that wakes every `flush_seconds` and
calls `_flush_all()`, and an inline size-check after every `emit()`.
Neither path blocks the poll loop: the actual `s3.put_object` call is
offloaded to a thread via `asyncio.to_thread`, so polling continues
while bytes are going over the wire.

The writer is shared with `scripts/live/kalshi_ws/` — any tuning of
`flush_seconds` or `flush_bytes` applies to both ingesters.

## What's written to S3

Bucket: `prediction-markets-data`. Prefix: `bronze/nba_cdn/{channel}/`.
Time-partitioned keys, gzipped JSONL, one file per bronze flush:

```
bronze/nba_cdn/scoreboard/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/nba_cdn/odds/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/nba_cdn/live_pbp/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/nba_cdn/boxscore/YYYY/MM/DD/HH/{uuid}.jsonl.gz
```

UUID keeps flushes collision-free across restarts and parallel
writers; `YYYY/MM/DD/HH` is the flush time (UTC), not the frame time.

## Record schema

One JSON object per line. Common fields on every record:

```json
{
  "source":    "nba_cdn",
  "channel":   "<scoreboard|odds|live_pbp|boxscore>",
  "t_request": 1713456789.123,
  "t_receipt": 1713456789.456,
  "frame":     { /* untouched response body */ }
}
```

Channel-specific additions:

| Channel      | Extra fields                                                  | Emission rule                                          |
|--------------|---------------------------------------------------------------|--------------------------------------------------------|
| `scoreboard` | —                                                             | Every tick                                             |
| `odds`       | —                                                             | Every non-304 response (etag-deduped)                  |
| `live_pbp`   | `game_id`, `action_number`, `poll_seq`; `frame` = one action | One record per action with `actionNumber > last_seen` |
| `boxscore`   | `game_id`                                                     | Every non-304 response (etag-deduped)                  |

`t_receipt` is the wall-clock time the response finished parsing and
is the ground truth for "when we knew" during backtest replay.

## Running it

Foreground:

```bash
source .venv/bin/activate
python -m scripts.live.nba_cdn
```

As a service on EC2: see
[`docs/live-nba-cdn-service.md`](../../../docs/live-nba-cdn-service.md).

## Known limitations

- **No on-disk resume.** Crash window is whatever's in memory — up to
  60 s or 5 MB of records per channel. Accepted tradeoff per
  `docs/data-flow.md`.
- **No correction detection.** NBA occasionally rewrites past PBP
  actions (e.g., block → goaltend after review). The current script
  emits each action on first sight and never re-inspects. Deferred
  per `docs/data-flow.md` open question #6.
- **Sequential per-game fetches.** If 20+ games go live concurrently
  the tick could exceed 3 s. Not observed yet; revisit if it happens.
