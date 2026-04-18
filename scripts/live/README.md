# `scripts/live/` — live ingesters

One long-running process per data source. Each process owns its wire
connection, a `BronzeWriter`, and (later) a transform + strategy + silver
writer. Design: [`docs/data-flow.md`](../../docs/data-flow.md). Deployment:
[`docs/live-nba-cdn-service.md`](../../docs/live-nba-cdn-service.md).

## `nba_cdn.py`

Polls `cdn.nba.com` and archives raw frames to
`s3://prediction-markets-data/bronze/nba_cdn/`. No transform, no silver —
bronze only.

### How it works

Single async event loop in `Ingester.run()`:

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
`gameStatus=2` game the scoreboard reports gets added to `self.games`;
anything that transitions to `gameStatus=3` is finalized and removed.
Restart starts from an empty game set and rediscovers on the first tick.

### What's parallelized

Not much within a tick — HTTP fetches inside `_tick()` are awaited
sequentially. The async model buys three things:

1. **Background bronze flushes.** `BronzeWriter._flush_loop` runs as a
   separate task, flushing buffers every 60 s independent of what the
   ingester is doing.
2. **Non-blocking S3 writes.** Each flush offloads `s3.put_object` to a
   thread (`asyncio.to_thread`), so the event loop keeps servicing ticks
   while bytes are going over the wire.
3. **Fire-and-forget `emit()`.** `bronze.emit()` just appends to an
   in-memory buffer — it never waits on S3. The tick loop is bounded by
   HTTP latency, not S3 latency.

What is **not** parallelized: PBP and boxscore fetches across live games
run in sequence. With ~10 concurrent games and ~50 ms RTT each, a full
tick is ~1 s well under the 3 s interval — so the simplicity wins.
Revisit if tick time ever approaches the interval.

### Poll cadence

`_tick()` chooses the next sleep based on today's scoreboard:

| Scoreboard state                          | Tick interval | Source constant          |
|-------------------------------------------|---------------|--------------------------|
| Any game `gameStatus=2` (live)            | **3 s**       | `POLL_INTERVAL`          |
| Games scheduled or recently final, none live | **60 s**   | `SCOREBOARD_INTERVAL_ACTIVE` |
| Empty slate (off-day, off-season)         | **300 s**     | `SCOREBOARD_INTERVAL_IDLE` |

Scoreboard + odds are fetched once per tick. PBP + boxscore are fetched
once per tick **per live game**. Etags on PBP/odds/boxscore mean 304
responses are free — no emission, no buffer growth.

### What's written to S3

Bucket: `prediction-markets-data`. Prefix: `bronze/nba_cdn/{channel}/`.
Time-partitioned keys, gzipped JSONL, one file per bronze flush:

```
bronze/nba_cdn/scoreboard/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/nba_cdn/odds/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/nba_cdn/live_pbp/YYYY/MM/DD/HH/{uuid}.jsonl.gz
bronze/nba_cdn/boxscore/YYYY/MM/DD/HH/{uuid}.jsonl.gz
```

UUID keeps flushes collision-free across restarts and parallel writers;
`YYYY/MM/DD/HH` is the flush time (UTC), not the frame time.

**Flush triggers** (from `BronzeWriter` defaults):
- 5 MB of uncompressed JSONL in a single channel buffer
- 60 s elapsed since the last flush (timer task)
- process shutdown (SIGINT/SIGTERM drains all buffers)

### Record schema

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

| Channel     | Extra fields                                           | Emission rule                               |
|-------------|--------------------------------------------------------|---------------------------------------------|
| `scoreboard`| —                                                      | Every tick                                  |
| `odds`      | —                                                      | Every non-304 response (etag-deduped)       |
| `live_pbp`  | `game_id`, `action_number`, `poll_seq`; `frame` = one action | One record per action with `actionNumber > last_seen` |
| `boxscore`  | `game_id`                                              | Every non-304 response (etag-deduped)       |

`t_receipt` is the wall-clock time the response finished parsing and is
the ground truth for "when we knew" during backtest replay.

### Running it

Foreground:

```bash
source .venv/bin/activate
python -m scripts.live.nba_cdn
```

As a service on EC2: see
[`docs/live-nba-cdn-service.md`](../../docs/live-nba-cdn-service.md).

### Known limitations

- **No on-disk resume.** Crash window is whatever's in memory — up to
  60 s or 5 MB of records per channel. See `docs/data-flow.md` for the
  accepted tradeoff vs. the old `scripts/nba_cdn/poll_live.py`.
- **No correction detection.** The old poller wrote periodic snapshot
  records with action hashes to detect retroactive NBA edits. Not yet
  ported; deferred per `docs/data-flow.md` open question #6.
- **Sequential per-game fetches.** If 20+ games go live concurrently the
  tick could exceed 3 s. Not observed yet; revisit if it happens.
