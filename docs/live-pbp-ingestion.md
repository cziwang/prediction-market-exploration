# Live Play-by-Play Ingestion

## Goal

Poll `cdn.nba.com` play-by-play data during live NBA games, recording arrival timestamps, so we can replay the exact information stream our trading model would have seen at any point during a game. This enables realistic backtesting against Kalshi NBA markets.

## What backtesting needs

To simulate trading decisions, we need to reconstruct what we **knew** and **when we knew it**:

- **Score state**: which team scored, running score, at what game clock
- **Game clock**: period + clock string (for market expiry alignment)
- **Status transitions**: start of period, halftime, end of game
- **Our receipt time**: wall-clock time we first observed each event (`t_receipt`)
- **Request timing**: when we sent the HTTP request (`t_request`), so we can measure RTT

We do **not** need sub-second precision on every rebound and turnover. The CDN updates every 2-5 seconds regardless.

## Architecture

No database. No message broker. Append to local files during the game, upload to S3 when the game ends. Fits the existing data pipeline.

```
┌──────────────────────────────┐
│  poll_live.py                │
│                              │
│  1. fetch scoreboard         │
│  2. for each active game:    │
│     - poll PBP every ~3s     │
│     - append new actions     │
│       to local JSONL         │
│  3. on game final:           │
│     - gzip + upload to S3    │
│     - delete local file      │
└──────────────────────────────┘
         │
         ▼
  S3: nba_cdn/live_pbp/{game_id}.jsonl.gz
         │
         ▼
  Backtest: load JSONL, replay events in t_receipt order
```

Single async Python script. One process handles all concurrent games (at most ~15 on a busy night). No scheduler/poller separation — just a loop.

## Data format

One JSONL file per game. Each line is one PBP action, first-seen by the poller:

```json
{
  "game_id": "0022501234",
  "action_number": 42,
  "t_request": 1713456789.123,
  "t_receipt": 1713456789.456,
  "poll_seq": 17,
  "action": { /* raw NBA action object, unmodified */ }
}
```

| Field | Purpose |
|---|---|
| `game_id` | NBA game identifier (e.g. `0052500211`) |
| `action_number` | NBA's sequence number; dedup key |
| `t_request` | `time.time()` before HTTP call; marks when we asked |
| `t_receipt` | `time.time()` after response parsed; our ground truth for "when we knew" |
| `poll_seq` | Monotonic counter per game; lets us reconstruct poll boundaries |
| `action` | Full raw action JSON from NBA; schema may change season to season |

### Why store the full raw action

Extracting only score/clock fields now would be simpler, but NBA's schema shifts between seasons and we don't yet know exactly which fields the trading model will need. Raw JSON is cheap (~0.5KB per action, ~500 actions per game = ~250KB uncompressed per game). Store raw, extract at query time.

## S3 layout

```
nba_cdn/
  live_pbp/
    {game_id}.jsonl.gz          # timestamped PBP stream
```

Fits alongside existing prefixes (`nba_cdn/scoreboard/`, `nba_cdn/play_by_play/`, etc.). The `live_pbp/` prefix distinguishes polled-with-timestamps data from the one-shot fetches in `play_by_play/`.

## Polling strategy

**Interval: 3 seconds.** The CDN updates every 2-5 seconds. Polling at 1s wastes ~60% of requests on identical responses. Polling at 3s catches most updates within one cycle. Can tighten later if measured edge loss justifies it.

**ETag / If-None-Match.** Send the previous response's `ETag` header back. CDN returns 304 Not Modified if nothing changed — less bandwidth, and we learn exactly when data updates.

**Dedup via `actionNumber`.** Track the max `actionNumber` seen per game. Only append actions with `actionNumber > last_seen`. NBA's `actionNumber` is monotonically increasing within a game.

**Round-robin, not per-game tasks.** Single loop iterates over all active games:

```
while active_games:
    for game_id in active_games:
        poll(game_id)
    sleep(interval)
```

With 10 concurrent games and 50ms RTT each, one full round takes ~500ms, well within the 3s budget. No need for per-game async tasks, cancellation logic, or task management.

## Handling replay corrections

NBA sometimes retroactively modifies past actions (e.g., changes a block to a goaltend after video review). Since we store actions append-only on first sight, corrections are invisible.

**Mitigation: periodic full snapshots.** Every 60 seconds (every ~20th poll), store a special snapshot line:

```json
{
  "game_id": "0022501234",
  "snapshot": true,
  "t_receipt": 1713456849.789,
  "poll_seq": 37,
  "total_actions": 142,
  "actions_hash": "sha256:abcdef...",
  "score": {"home": 54, "away": 48},
  "period": 2,
  "clock": "PT04M22.00S",
  "game_status": 2
}
```

During backtest replay, if the running hash diverges from the snapshot hash, we know a correction happened between snapshots. For v1 this is informational only — flag it but don't try to reconcile automatically.

## Game lifecycle

```
Scheduled (status=1)  →  poll scoreboard every 60s, waiting
In Progress (status=2) →  poll PBP every 3s, appending to JSONL
Final (status=3)       →  one last PBP poll, gzip, upload to S3, cleanup
```

**Detecting active games:** Fetch `todaysScoreboard_00.json` every 60 seconds. When a game transitions to status 2, start polling its PBP. When it transitions to status 3, finalize.

**Late games / date boundaries:** Games starting at 10pm ET may end past midnight UTC. The poller doesn't care about dates — it tracks game IDs, not calendar days. The scoreboard endpoint always returns today's games in the local CDN sense.

**Crash recovery:** On restart, scan `data/live_pbp/` for existing JSONL files. For each, read the last line to get `last_action_number`, then resume polling. Append-only JSONL makes this trivial.

## Continuous operation

The script runs as a **long-lived process** — not invoked per game or per night. It polls the scoreboard continuously, discovers games on its own, and handles the full lifecycle without manual intervention.

### Daily rhythm

The NBA schedule is predictable: games are typically between 7pm–1am ET. But the poller doesn't hardcode windows. It simply polls the scoreboard at different cadences depending on whether games are active:

```
No active games, none on scoreboard  →  poll scoreboard every 5 min
Games on scoreboard but not started  →  poll scoreboard every 60s
Games in progress                    →  poll scoreboard every 60s + PBP every 3s
All games final                      →  upload, then back to 5 min cadence
```

This means idle overnight polling costs ~288 requests/day (every 5 min × 24h) — negligible.

### Process management

**On EC2 (recommended):** Run as a systemd service. See "EC2 deployment" section below.

**On laptop (for validation):** Just run it in a tmux/screen session. Same script, no changes needed.

### Off-season

During the off-season (July–October), the scoreboard returns an empty game list. The poller idles at 5-minute scoreboard checks, costing effectively nothing. No need to stop/start it seasonally.

### Graceful shutdown

On SIGTERM/SIGINT: finish the current poll cycle, flush any buffered writes, then exit. Do **not** upload partial game files to S3 — they'll be resumed on restart. Only upload on game final.

## Local file management

```
data/
  live_pbp/
    {game_id}.jsonl       # active game, being appended to
```

- Created when a game enters status 2
- Appended to on each poll with new actions
- On game final: gzip → upload to S3 → delete local file
- `data/live_pbp/` is gitignored

## What this does NOT include

- **Database.** Not needed. JSONL files are the event log. DuckDB or pandas can query them directly for backtesting.
- **Live consumer / model interface.** Build this when there's a model to consume. The file format supports tailing (`tail -f`) if needed.
- **Monitoring / alerting.** Log to stdout for now. Add structured logging or metrics when running on a VM.
- **Kalshi data alignment.** Separate concern. The backtest harness will join PBP events with Kalshi trade/candlestick data by timestamp.
- **Odds ingestion.** `cdn.nba.com/odds` is a separate endpoint with different update cadence. Can poll it in the same loop later if needed.

## Dependencies

Already in the project:
- `httpx` (async HTTP, used by `nba_cdn.py`)
- `boto3` (S3 uploads, used by `s3_raw.py`)

New:
- None. `asyncio`, `gzip`, `hashlib`, `json`, `time` are stdlib.

## Deployment

**Phase 1 (now):** Run on laptop in tmux. Validate data for a few game nights — check for gaps, measure CDN update cadence, verify crash recovery works.

**Phase 2 (when proven):** Move to EC2 with systemd (see below). Runs unattended, restarts on crash/reboot. ~$5-10/mo.

**Phase 3 (if needed):** Add Postgres for live query access, Redis for low-latency fanout to a live trading model. Only if the file-based approach becomes a bottleneck.

## EC2 deployment

### Instance setup

- **Instance type:** `t4g.micro` (~$3/mo with free tier, ~$8/mo without). ARM — cheap and sufficient.
- **Region:** `us-east-1`. NBA CDN is served via CloudFront; east-coast AWS gets 10-30ms RTT.
- **AMI:** Amazon Linux 2023 ARM or Ubuntu 24.04 ARM. Either works — pick whichever you prefer.
- **Storage:** 8GB gp3 (default). JSONL files are small and get deleted after S3 upload.
- **Security group:** SSH inbound only. No HTTP needed.
- **IAM instance profile:** Attach a role with `s3:PutObject` on `arn:aws:s3:::prediction-markets-data/*`. This avoids storing AWS credentials on the instance.

### Bootstrap

```bash
ssh -i your-key.pem ec2-user@<instance-ip>   # or ubuntu@ for Ubuntu AMI

# System deps (Amazon Linux 2023)
sudo dnf install -y python3.13 git

# System deps (Ubuntu 24.04)
# sudo apt update && sudo apt install -y python3.13 python3.13-venv git

# Clone and install
git clone <your-repo-url> ~/prediction-market-exploration
cd ~/prediction-market-exploration
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Smoke test
.venv/bin/python -m scripts.nba_cdn.poll_live
# Ctrl+C after you see scoreboard polling
```

### systemd service

```bash
sudo tee /etc/systemd/system/nba-poller.service > /dev/null <<'EOF'
[Unit]
Description=NBA Live PBP Poller
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/prediction-market-exploration
ExecStart=/home/ec2-user/prediction-market-exploration/.venv/bin/python -m scripts.nba_cdn.poll_live
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable nba-poller   # start on boot
sudo systemctl start nba-poller    # start now
```

Adjust `User` and paths if using Ubuntu (`ubuntu` instead of `ec2-user`).

### Operations

```bash
# Status
sudo systemctl status nba-poller

# Logs (live tail)
journalctl -u nba-poller -f

# Check for data files during a game
ls -la ~/prediction-market-exploration/data/live_pbp/

# Deploy updates
cd ~/prediction-market-exploration && git pull
sudo systemctl restart nba-poller
```

`Restart=always` + crash recovery (resume from last `actionNumber` in JSONL) means the process self-heals. A crash mid-game loses at most one poll cycle (~3s of events), then picks up where it left off.

## Open questions

1. **Poll interval tuning.** Is 3s optimal? Need to measure CDN update frequency empirically during a few games and decide.
2. **Boxscore polling.** Should we also poll boxscores (team/player stats) on the same cadence? Useful if the model cares about player-level stats, but adds HTTP calls per game.
3. **Scoreboard snapshots.** Should we store timestamped scoreboard snapshots too? They contain score + clock without needing per-game PBP parsing. Could be a lightweight alternative for score-only models.
4. **Historical gap.** We have Kalshi historical data from April 2025 – Feb 2026, but no live PBP data from that period (only one-shot fetches without timestamps). The polled data starts now. Need to decide if approximate timestamps from one-shot data are useful for backtesting or if we only backtest on the polled window.
