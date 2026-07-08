# Testing Guide

Onboarding doc for verifying the Phase 1 pipeline. This walks through every
layer of testing, explains the tools involved, and shows how to replicate
each verification from a fresh clone. No prior familiarity with Kafka,
Flink, or ClickHouse is assumed.

If you only have five minutes: read [The mental model](#the-mental-model),
then run the [Quick verification](#quick-verification-5-minutes).

---

## The mental model

The pipeline replays historical market data as if it were live:

```
S3 bronze files ──► Replayer ──► Kafka ──► Flink join ──► Kafka ──► ClickHouse
(raw archives)      (Python)    (message   (enriches      (output    (SQL
                                 bus)       trades with    topic)     database)
                                            game state)
```

- **Bronze** = raw gzip-JSONL files in S3. Two kinds: Kalshi prediction-market
  trades and NBA play-by-play actions. Every record has a `t_receipt`
  timestamp — when our collector received it during the 2026 playoffs.
- **Replayer** (`pm/replay/`) reads bronze files, merges them into one
  time-ordered stream, and publishes to Kafka *with the original timestamps* —
  so downstream consumers can't tell replayed history from a live feed.
- **The Flink job** (`pm/enrich/`) joins each Kalshi trade with the NBA game
  state at that moment ("as-of join") and computes model features.
- **ClickHouse** stores the enriched output for SQL analysis.

Testing philosophy: the hard logic (the join, the merge) is **pure Python**
with no framework dependencies, so it's tested with fast unit tests. The
infrastructure glue (Kafka, Flink, ClickHouse) is verified with end-to-end
runs against a **golden fixture** — one real game whose facts we verified by
hand against ESPN.

---

## Tool primer (skip what you know)

| Tool | What it is | How we use it |
|------|-----------|---------------|
| **pytest** | Python test runner | `python -m pytest tests/` runs everything in `tests/` |
| **Hypothesis** | Property-based testing library | Instead of hand-picking test inputs, you state a property ("merged output is always sorted") and Hypothesis generates hundreds of adversarial inputs trying to falsify it |
| **Docker Compose** | Runs services in containers from `docker-compose.yml` | `docker compose up -d` starts Kafka + ClickHouse locally |
| **Kafka** | Distributed message log ("topics" hold ordered streams of records) | The pipeline's data bus; the replayer produces to it, Flink consumes from it |
| **PyFlink** | Python API for Apache Flink, a stream processor | Runs the as-of join with event-time semantics |
| **ClickHouse** | Columnar SQL database built for time-series analytics | The queryable tick store at the end of the pipeline |

---

## Prerequisites

```bash
# 1. Python env (3.11 — PyFlink requires <= 3.12)
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install 'apache-flink==1.20.5'
pip install -e ".[dev]"

# 2. The Flink Kafka connector jar (a Java library Flink loads at runtime;
#    it is gitignored — fetch it once)
mkdir -p jars
curl -L -o jars/flink-sql-connector-kafka-3.4.0-1.20.jar \
  https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.4.0-1.20/flink-sql-connector-kafka-3.4.0-1.20.jar

# 3. Services (Docker Desktop must be running)
docker compose up -d
docker compose ps   # wait for kafka and clickhouse to show "healthy"
```

AWS credentials are **not** needed for any test below — the golden fixtures
are committed to the repo. (S3 access is only needed to regenerate fixtures
or replay other games.)

---

## Layer 1 — Unit tests (fast, no services needed)

```bash
python -m pytest tests/ -v
```

**Expected: 68 passed, under 1 second.** These run against local code and
the committed golden fixtures only — no Kafka, no Docker, no network.

What's covered, by directory:

### `tests/replay/`
- **`test_sources.py`** — reading bronze `.jsonl.gz` files: correct record
  counts against the golden fixture (548 play-by-play actions, 20,376
  trades), correct Kafka topic/key assignment, and that malformed lines
  raise `SourceError` with the exact file and line number (bronze is data we
  control — a bad line means investigate, not skip).
- **`test_merge.py`** — the k-way merge that interleaves multiple bronze
  files into one time-ordered stream. Includes a Hypothesis property test:
  *any* collection of sorted sources must merge to sorted output. Also
  verifies `OrderingError` fires if a source's timestamps go backward —
  see [war story #1](#war-story-1-the-shuffled-merge).
- **`test_producer.py`** — the in-memory producer used for testing, and the
  topic/partition configuration (4/4/1/1) matching DESIGN.md.
- **`test_replayer.py`** — orchestration: record counts per topic, key
  correctness, Kafka timestamps non-decreasing per topic, and pacing math
  (`speed_multiplier=1.0` sleeps to reproduce real-time gaps; tested with a
  fake clock, no actual sleeping).

### `tests/kalshi/` and `tests/nba/`
- **Normalizers** — raw bronze JSON → typed events. Every golden line must
  normalize; malformed input must return a `Dlq` result (dead-letter),
  **never raise**. Hypothesis fuzzes with random bytes to prove no input can
  crash a normalizer.
- **`test_ticker.py`** — parsing `KXNBAGAME-26APR18ATLNYK-NYK` into
  away/home teams and which side YES pays.

### `tests/enrich/`
- **`test_features.py`** — the win-probability model: tied game = 0.5,
  symmetric for leads/deficits, more certain as time runs out, exact
  boundaries at zero seconds.
- **`test_join.py`** — the heart of the system, the dual-view as-of join.
  Read this file if you read nothing else. Key tests:
  - `test_no_future_leak` — a trade at t=3 must be enriched with the game
    state received at t=1, **not** a state received at t=5, even when both
    states arrive before the trade is finalized. This is why the join
    buffers *both* streams and processes them in timestamp order.
  - `test_arrival_order_does_not_matter` + a Hypothesis test over all
    permutations of 6 records — the join's output depends only on
    timestamps, never on network arrival order. This property is what makes
    replayed backtests trustworthy.
  - `test_views_diverge_under_cdn_lag` — the "receipt view" (what our
    system knew) vs "event view" (what had happened on court) behave
    differently under polling lag, by design.

---

## Layer 2 — The golden game

Everything end-to-end is verified against **one real game**:

- **Game `0042500121`** — Atlanta Hawks @ New York Knicks, 2026-04-18,
  final NYK 113–102. Playoff Game 1.
- We verified its facts by hand against ESPN and Basketball-Reference:
  final score, home team, player stats, tip-off time.
- Its raw data is frozen in `tests/fixtures/golden/` (committed to git —
  binary fixtures are intentional here):
  - `nba_pbp_0042500121.jsonl.gz` — all 548 play-by-play actions
  - `kalshi_trades_0042500121.jsonl.gz` — all 20,376 Kalshi trades on that
    game (coverage starts mid-game at 00:15 UTC — trade collection began
    late that day; this is deliberate test surface for "trades with no
    earlier game state")
  - `enriched_0042500121.jsonl.gz` — the **frozen expected output** of the
    entire pipeline

Why one game? Because a fixture you've verified by hand is worth more than
a thousand rows you haven't. Every anchor check below traces to a fact a
human confirmed against an external source.

---

## Quick verification (5 minutes)

From a fresh clone with prerequisites done:

```bash
source .venv/bin/activate

# 1. Unit tests
python -m pytest tests/ -q                     # expect: 68 passed

# 2. Replay the golden game into Kafka
python -m pm.replay --source golden
# expect: replayed 20,924 records (20,376 trades + 548 game states)

# 3. Run the Flink enrichment job (takes ~1 min; bounded mode reads the
#    topics to their current end and then exits)
python -m pm.enrich.job

# 4. Verify output parity against the frozen golden output
python scripts/golden_output.py --verify
# expect: 9 anchor PASSes, then
# PARITY OK: output matches frozen fixture (20,376 records)

# 5. Sink to ClickHouse and query it
python -m pm.sink.clickhouse --once            # expect: inserted 20,376 rows
docker compose exec clickhouse clickhouse-client --user pm --password pm \
  --query "SELECT count(), round(avg(r_info_delay_ms)/1000,1) FROM pm.enriched_trades"
```

**Caveat for step 2:** if the Kafka topics already contain data from a
previous replay, records will append and counts will be off. Do a clean
wipe first (next section) when in doubt.

---

## Layer 3 — The parity test (the most important one)

**Claim being tested:** the pipeline is *deterministic* — same input, same
output, byte for byte, regardless of when or how many times you run it.
This is the foundation of the Kappa architecture: if replay isn't
deterministic, backtests can't be trusted.

**Procedure** (wipe everything → rebuild from scratch → compare):

```bash
# 1. Delete all topics
for t in kalshi.trades kalshi.book_update nba.game_state reference.markets enriched.trades dlq.enrich; do
  docker compose exec kafka /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server localhost:9092 --delete --topic $t 2>/dev/null
done

# 2. WAIT for deletion to complete — Kafka topic deletion is ASYNCHRONOUS.
#    If you skip this, the next step races the deletion and you get partial
#    data (we hit this; see war story #4).
python - <<'EOF'
from confluent_kafka.admin import AdminClient
import time, sys
admin = AdminClient({'bootstrap.servers': 'localhost:9092'})
targets = {'kalshi.trades','kalshi.book_update','nba.game_state',
           'reference.markets','enriched.trades','dlq.enrich'}
for _ in range(60):
    if not (set(admin.list_topics(timeout=5).topics) & targets):
        print('deletion complete'); sys.exit(0)
    time.sleep(1)
sys.exit('deletion did not complete')
EOF

# 3. Replay + join + verify
python -m pm.replay --source golden
python -m pm.enrich.job
python scripts/golden_output.py --verify
```

**Expected output:**

```
consumed 20,376 records from enriched.trades

anchor checks:
  PASS  record count == 20376
  PASS  all records belong to golden game
  PASS  every record has game state attached (trades cover late game only)
  PASS  info delay non-negative wherever present
  PASS  trades exist after final buzzer
  PASS  all post-buzzer trades see final score diff 11 (NYK 113-102)
  PASS  KXNBAGAME trades have model prob
  PASS  NYK (winner, diff=+11) model prob = 1.0 at buzzer
  PASS  non-GAME series have no model prob (not applicable)

PARITY OK: output matches frozen fixture (20,376 records)
```

The anchor checks assert facts traceable to the hand-verified game
(score diff 11 = the real 113–102 final; the winner's model probability is
exactly 1.0 once the game ends). The parity check then compares all 20,376
records byte-for-byte against the frozen fixture, after a deterministic
sort (Kafka partition *consumption* order varies run to run; the record
*content* must not).

**If parity breaks after your change:** either you introduced a regression
(fix it) or you intentionally changed output semantics — in which case
re-freeze with `python scripts/golden_output.py --freeze`, re-verify the
anchors, and say so explicitly in your PR.

---

## Layer 4 — DLQ test (bad data doesn't vanish, doesn't crash)

**Claim being tested** (DESIGN.md D8): a malformed record neither crashes
the job nor silently disappears — it lands in the `dlq.enrich` topic with
the raw bytes and the reason.

Procedure: do the wipe + wait + replay from Layer 3, then **inject garbage
before running the job**:

```bash
python - <<'EOF'
from pm.replay.producer import KafkaProducer, ProducerRecord
p = KafkaProducer('localhost:9092')
p.produce(ProducerRecord('kalshi.trades', 'garbage', b'{not json at all', 1776552000000))
p.produce(ProducerRecord('kalshi.trades', 'badticker',
    b'{"source":"kalshi_ws","channel":"trade","t_receipt":1776552000.0,'
    b'"frame":{"sid":1,"seq":1,"msg":{"market_ticker":"WEIRD!!",'
    b'"yes_price_dollars":"0.50","no_price_dollars":"0.50",'
    b'"count_fp":"1.00","taker_side":"yes"}}}', 1776552000000))
p.flush()
EOF

python -m pm.enrich.job
python scripts/golden_output.py --verify     # parity must STILL pass
```

Then inspect the DLQ:

```bash
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 --topic dlq.enrich \
  --from-beginning --timeout-ms 5000
```

**Expected:** exactly 2 DLQ records — one `JSONDecodeError`, one
`unparseable ticker: WEIRD!!` — each carrying the original raw line. And
the parity check still passes: garbage in the input stream did not perturb
the join output.

Note the three-way classification in `pm/enrich/job.py`: `OK` (join it),
`DLQ` (data error), `SKIP` (valid market for a game we're not tracking —
expected, not an error). Only genuine errors go to the DLQ.

---

## Layer 5 — ClickHouse idempotency test (duplicates can't happen)

**Claim being tested** (DESIGN.md D3): the pipeline is at-least-once — a
crash between processing and offset-commit causes *reprocessing*, which
would insert duplicate rows unless the sink is idempotent.

How the sink achieves idempotency (`pm/sink/clickhouse.py`):
1. Batches are **offset-aligned**: a batch flushes when it hits an offset
   boundary (`(offset+1) % batch_size == 0`), so batch boundaries are the
   same on every run — they depend on offsets, not timing.
2. Every insert carries `insert_deduplication_token =
   "{topic}-{partition}-{first_offset}-{last_offset}"`. Reprocessing forms
   the identical batch → identical token → ClickHouse drops it.
3. Kafka offsets are committed only **after** a successful insert.
4. Gotcha encoded in the DDL: non-replicated MergeTree tables **ignore**
   dedup tokens unless `non_replicated_deduplication_window` is set.

Procedure (simulates total loss of consumer progress — the worst-case
crash recovery):

```bash
python -m pm.sink.clickhouse --once                          # first load
# fresh group id = no committed offsets = reprocess EVERYTHING from offset 0
python -m pm.sink.clickhouse --once --group-id ch-sink-rerun # full reprocess

docker compose exec clickhouse clickhouse-client --user pm --password pm \
  --query "SELECT count() FROM pm.enriched_trades"
```

**Expected: `20376` both before and after the re-run.** The second run
consumes and re-inserts all 20,376 rows; ClickHouse silently drops every
batch. If you see 40,752, dedup is broken — first thing to check is the
`non_replicated_deduplication_window` setting on the table.

Known accepted corner (documented in the sink's docstring): the final
*partial* batch in `--once` mode is bounded by end offsets at run time; a
crash racing new arrivals could re-form that tail differently and duplicate
its overlap. Full batches — the case that matters for checkpoint-style
reprocessing — are exactly idempotent.

---

## War stories — why these tests exist

Each of these was a real bug found during Phase 1. They're kept here
because they teach the failure modes this codebase defends against.

### War story #1: the shuffled merge
The very first run of the merge ordering check (`OrderingError`) against
real data failed with a 25-minute backward time jump. Cause: the script
that consolidated bronze files listed S3 keys *lexicographically* — and
within an hour prefix, filenames are random UUIDs, so ~60-second chunks
were concatenated in alphabetical, not chronological, order. **Every**
consolidated Kalshi file in S3 was affected. Without the check, Flink's
watermarks would have silently classified half the data as "late" and
dropped it. Lesson: *verify ordering assumptions; never trust listing
order as time order.*

### War story #2: Kafka ate the replay
The first Flink run read zero records from topics we had just filled.
Cause: replayed messages carry their **original April timestamps**, and
Kafka's time-based retention (default 7 days) judges age by message
timestamp — so it deleted our "3-month-old" replay minutes after we
produced it. Fix: `retention.ms=-1` on replay topics. Lesson: *time-based
retention uses event time, not arrival time — a replay-architecture
classic.*

### War story #3: the sloppy verification
While debugging #2, an early check "confirmed" data was present by summing
high watermark offsets. High watermarks count everything *ever produced*;
only `high - low` counts what's *currently present*. The data was already
deleted. Lesson: *know what your metric actually measures.*

### War story #4: the async deletion race
A parity run failed with partial output. Cause: Kafka topic deletion is
asynchronous — the job re-created its output topic while the old one was
still being torn down, wrote into it, and lost records mid-deletion. Fix:
poll `list_topics()` until the names are actually gone before proceeding
(step 2 of the Layer 3 procedure). Lesson: *distributed deletes are not
synchronous; "the command returned" ≠ "it happened."*

### War story #6: action order is not receipt order
The full-dataset replay aborted with `OrderingError` on game `0042500115`:
a 94-minute backward jump in receipt time. Cause: the PBP consolidation
script sorted by `action_number` (the NBA's play counter) — but the CDN
sometimes re-delivers an *edit* to an early action long after it happened,
so a low action_number can arrive an hour later. Sorting by action number
put that late arrival back among the early records. Fix: sort merged PBP by
`(t_receipt, action_number)` — receipt order is the contract, action number
only breaks ties within a poll. The golden game passed earlier only because
it happened to contain no late edits. Lesson: *there are usually more
orderings in a dataset than you think (event order, delivery order, edit
order) — be explicit about which one you're sorting by and which one your
consumers require.*

### War story #5: zstd
The Flink job crashed with `NoClassDefFoundError: ...ZstdOutputStream...`.
The replayer compressed Kafka batches with zstd; Flink's shaded Kafka
connector doesn't bundle the zstd native library. Fix: lz4. Lesson:
*compression is baked into stored Kafka batches at produce time — consumer
compatibility is a producer-side decision.*

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `docker compose` errors: cannot connect to daemon | Docker Desktop not running | `open -a Docker`, wait, retry |
| Flink job: `ModuleNotFoundError: pyflink` in a Java stack trace | Flink workers using system python | The job sets `set_python_executable(sys.executable)` — make sure you're running with `.venv/bin/python` |
| Flink job instantly "succeeds" with 0 output | Topics empty (see war story #2/#3) — check `high - low`, not high | Re-replay; confirm topics were created by our code (which sets `retention.ms=-1`) |
| Replay counts double what you expect | Topics not wiped before re-replay (append semantics) | Layer 3 wipe + wait procedure |
| Parity fails right after topic deletion | Async deletion race (war story #4) | Use the wait loop; never skip step 2 |
| ClickHouse row count doubled | Dedup token ignored | Check table has `non_replicated_deduplication_window` setting; batches must be offset-aligned |
| `SourceError` reading a bronze file | Genuinely malformed/misordered bronze | Investigate the file at the reported line — do not "fix" by skipping |

---

## Layer 6 — Full backfill verification (Phase 2, in progress)

The full backfill runs all 60 playoff games (2026-04-18 → 2026-05-11)
through the same pipeline. Sequence (see DESIGN.md Build Order for status):

```bash
# game map (event_code -> game_id for all 60 games; validated against the
# golden anchor — the build FAILS if 26APR18ATLNYK doesn't map to 0042500121)
python scripts/build_game_map.py

# wipe topics + wait (Layer 3 procedure), then:
python -m pm.replay --source s3 --start 2026-04-18 --end 2026-05-11
python -m pm.enrich.job --game-map-file reference/game_map.json   # ~2.8M records, slow
python -m pm.sink.clickhouse --once --group-id ch-sink-backfill
```

Monitoring a long enrichment run: watch `enriched.trades` grow
(`get_watermark_offsets` high-low per partition), confirm `dlq.enrich`
stays 0, and tail the Flink log
(`.venv/lib/python3.11/site-packages/pyflink/log/flink-*-python-*.log`).
Periodic "Node disconnected" INFO lines are idle-connection reaping — benign.

Post-backfill verification is automated: `python scripts/verify_backfill.py`.
Result (2026-07-08): **all checks pass** — 5,851,077 rows, 0 DLQ, 0 duplicates,
settlement reality check green across all 42 games with post-buzzer trades
(winner's model prob exactly 1.0, loser's 0.0, winners derived independently
from the game map). Full-dataset info delay: avg 53s / p50 15s / p95 195s.

**Known data gaps (collection-time, not pipeline bugs):** 50 of 60 games have
trades. Missing: 9 games during a Kalshi-collector outage (2026-04-28 →
05-01 — raw bronze has zero trade files those days) and CLE-DET game 1
(0042500201) whose markets the collector never subscribed to (games 2–3 of
the same series are present). All 60 games have full PBP.

**Performance finding (FIXED):** the first full enrichment run took ~6 hours
(~270 rec/s). Root cause: the operator re-pickled the entire AsOfJoiner —
including every buffered trade — into ValueState on every element (O(n²)
serialization). Fixed with granular state (append-only ListState buffer,
small ValueState views, single next-timer per key); golden parity reproduced
byte-for-byte, golden-game wall time 65s → 8.8s. War story #7 in spirit:
*whole-object ValueState is the classic Flink Python anti-pattern; per-element
cost must be O(element), never O(state).*

**Cluster mode:** jobs can now run on the dockerized Flink cluster
(`docker compose up -d` brings up jobmanager + taskmanager with 4 slots;
`make submit-enrich` submits from inside the network at parallelism 4 with
60s checkpointing; Web UI at :8081). On EC2, tunnel with
`ssh -L 8081:localhost:8081 -L 8123:localhost:8123 ubuntu@<IP>`.
Cluster-vs-mini-cluster parity on the golden game is the pending acceptance
test, then a timed full backfill.

## Where to go next

- `DESIGN.md` — the architecture doc; D1–D9 are the key design decisions,
  and the "Two-Timestamp Problem" section explains the dual-view join
- `tests/enrich/test_join.py` — the executable specification of the join
- `tests/fixtures/golden/README.md` — fixture provenance and regeneration
