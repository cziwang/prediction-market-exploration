# Running `scripts/live/nba_cdn` as a systemd service

Deployment guide for the bronze-writing NBA CDN ingester at
`scripts/live/nba_cdn/`. Design lives in [`data-flow.md`](data-flow.md);
EC2 prerequisites in [`ec2-bootstrap.md`](ec2-bootstrap.md). The older
per-game-JSONL poller at `scripts/nba_cdn/poll_live.py` is deprecated —
left in the tree as a fallback with no active service backing it.

The service writes directly to
`s3://prediction-markets-data/bronze/nba_cdn/` via `BronzeWriter` — no
local files, continuous flushes every ~60 s.

## Prerequisites

Assumes the EC2 instance is already set up per
[`ec2-bootstrap.md`](ec2-bootstrap.md) (Python 3.12 venv,
`requirements.txt` installed, IAM role with S3 access attached). If
not, follow that doc first.

On the instance, pull latest code:

```bash
cd ~/prediction-market-exploration
git pull
```

## 1. Write the unit file

```bash
sudo tee /etc/systemd/system/nba-live.service > /dev/null <<'EOF'
[Unit]
Description=NBA Live Bronze Ingester (cdn.nba.com → S3 bronze)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/prediction-market-exploration
ExecStart=/home/ubuntu/prediction-market-exploration/.venv/bin/python -m scripts.live.nba_cdn
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

### Directive notes (differences from `nba-poller.service`)

- `KillSignal=SIGINT` — the script's signal handler listens for SIGINT/SIGTERM
  and flips a shutdown flag; the `async with BronzeWriter` drains buffers on
  exit. Matching the interactive Ctrl-C path keeps the code path identical.
- `TimeoutStopSec=30` — give `BronzeWriter` up to 30 s to flush any buffered
  frames to S3 before systemd escalates to SIGKILL.

Everything else (`Restart=always`, `RestartSec=10`, `User=ubuntu`, journald
logging) matches the older poller unit.

## 2. Register and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable nba-live
sudo systemctl start nba-live
```

## 3. Verify

```bash
# Should show "Active: active (running)"
sudo systemctl status nba-live

# Follow logs
journalctl -u nba-live -f

# Scoreboard objects should land within ~60s
aws s3 ls s3://prediction-markets-data/bronze/nba_cdn/scoreboard/ --recursive | tail

# live_pbp populates only while a game is in progress (gameStatus=2)
aws s3 ls s3://prediction-markets-data/bronze/nba_cdn/live_pbp/ --recursive | tail
```

## Operations cheatsheet

```bash
# Status / logs
sudo systemctl status nba-live
journalctl -u nba-live -f
journalctl -u nba-live --since "1 hour ago"

# Restart after pulling new code
cd ~/prediction-market-exploration && git pull
sudo systemctl restart nba-live

# Stop / start without changing boot behavior
sudo systemctl stop nba-live
sudo systemctl start nba-live

# Disable auto-start on boot (unit file remains on disk)
sudo systemctl disable nba-live
```

## Coexisting with the old `nba-poller` service

The two services write to **different S3 prefixes** and do not conflict:

| Service | Script | S3 prefix |
|---|---|---|
| `nba-poller` (old) | `scripts.nba_cdn.poll_live` | `nba_cdn/live_pbp/{game_id}.jsonl.gz` |
| `nba-live` (new) | `scripts.live.nba_cdn` | `bronze/nba_cdn/{channel}/YYYY/MM/DD/HH/*.jsonl.gz` |

Run them in parallel for a night or two if you want the old path as a safety
net while the new one is unproven. To retire the old one:

```bash
sudo systemctl stop nba-poller
sudo systemctl disable nba-poller
```

## Crash-recovery caveat

The old poller appended to local JSONL on disk and uploaded to S3 only at
`gameStatus=3`. On crash, restart resumed from the file's last
`action_number` — durability was game-level.

The new service buffers frames **in memory** in `BronzeWriter` and flushes
to S3 every 60 s (or 5 MB). With `Restart=always`, a crash loses up to
one flush window — ~60 s of records in the worst case. There is no
on-disk resume; on restart, any in-flight games are picked up from the
current scoreboard and polling resumes, but the gap during the restart
window is not recovered.

This is an accepted tradeoff per `data-flow.md` (bronze is the permanent
store, crash blast radius ≤ 60 s / 5 MB). If durability matters more than
the design currently assumes, revisit by either (a) lowering
`flush_seconds` on `BronzeWriter`, or (b) reintroducing a local append-only
log as a pre-bronze staging step.
