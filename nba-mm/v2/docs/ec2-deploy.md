# EC2 Deployment: v2 Live Ingester

Deploy `v2.scripts.live.kalshi_ws` as a systemd service on EC2.

## Prerequisites

- EC2 instance running Ubuntu 24.04 (t3.micro is sufficient)
- IAM role `prediction-markets-poller` attached (needs S3 read/write to `prediction-markets-data`)
- Python 3.12 venv at `~/prediction-market-exploration/.venv/`

### Kalshi credentials

Kalshi requires auth even for public WebSocket channels. Two secrets are needed:

- **`KALSHI_API_KEY_ID`** — UUID from Kalshi's API-keys UI
- **`KALSHI_PRIVATE_KEY_PATH`** — absolute path to the RSA `.pem` private key

Place the `.pem` outside any git-tracked directory and lock it down:

```bash
mkdir -p ~/prediction-market-exploration/keys
# copy the .pem via scp, cat-paste, etc.
chmod 600 ~/prediction-market-exploration/keys/kalshi-private-key.pem
```

Create/update `~/prediction-market-exploration/.env`:

```
KALSHI_API_KEY_ID=<uuid from kalshi>
KALSHI_PRIVATE_KEY_PATH=/home/ubuntu/prediction-market-exploration/keys/kalshi-private-key.pem
```

```bash
chmod 600 ~/prediction-market-exploration/.env
```

## 1. Pull code and install deps

```bash
cd ~/prediction-market-exploration
git pull
.venv/bin/pip install -r v2/requirements.txt
```

## 2. Smoke test

Run in the foreground first. Expect `subscribing ['orderbook_delta'] to N markets` within seconds, then `bronze flush` lines every ~60s:

```bash
.venv/bin/python -m v2.scripts.live.kalshi_ws
# Ctrl-C to stop — watch it drain buffers to S3 before exiting.
```

## 3. Write the systemd unit file

```bash
sudo tee /etc/systemd/system/kalshi-v2-live.service > /dev/null <<'EOF'
[Unit]
Description=Kalshi v2 Live Ingester (WebSocket → S3 bronze + silver)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/prediction-market-exploration
ExecStart=/home/ubuntu/prediction-market-exploration/.venv/bin/python -m v2.scripts.live.kalshi_ws
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

- **`KillSignal=SIGINT`** — matches Ctrl-C; the ingester's signal handler closes the WS and flushes both writers.
- **`TimeoutStopSec=30`** — grace period for BronzeWriter + SilverWriter to flush to S3 before SIGKILL.
- **`Restart=always` + `RestartSec=10`** — second line of defense behind the ingester's own exponential backoff reconnect.

## 4. Register and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable kalshi-v2-live
sudo systemctl start kalshi-v2-live
```

## 5. Verify

```bash
sudo systemctl status kalshi-v2-live
journalctl -u kalshi-v2-live -f
aws s3 ls s3://prediction-markets-data/bronze/kalshi_ws/ --recursive | tail
```

## Operations cheatsheet

```bash
# Status / logs
sudo systemctl status kalshi-v2-live
journalctl -u kalshi-v2-live -f
journalctl -u kalshi-v2-live --since "1 hour ago"

# Restart after pulling new code
cd ~/prediction-market-exploration && git pull
.venv/bin/pip install -r v2/requirements.txt
sudo systemctl restart kalshi-v2-live

# Stop / start without changing boot behavior
sudo systemctl stop kalshi-v2-live
sudo systemctl start kalshi-v2-live

# Disable auto-start on boot
sudo systemctl disable kalshi-v2-live
```

### Rotate Kalshi credentials

1. Create a new key in Kalshi's API-keys UI, download the new `.pem`
2. Replace the `.pem` on disk (`chmod 600`), update `KALSHI_API_KEY_ID` in `.env`
3. `sudo systemctl restart kalshi-v2-live`
4. Delete the old key in Kalshi's UI

## Operational notes

### Reconnect behavior

The ingester reconnects with exponential backoff (1s initial, 60s max). Reconnect triggers a fresh REST lookup of open markets and a new subscribe cycle — producing a fresh `orderbook_snapshot` per market.

If all series have zero open markets (off-season, deep off-hours), the loop idles for 300s before re-checking.

### Crash-recovery caveat

BronzeWriter buffers in memory and flushes every 60s or 5 MB. A crash loses up to one flush window (~1 minute of records). No on-disk staging. On reconnect, fresh snapshots restore book state — but missed deltas are gone from bronze.

### Secrets hygiene

`.env` and `keys/` are gitignored. Never commit either. If you accidentally do, rotate the Kalshi key immediately and rewrite git history.

---

## Daily Silver Compaction (systemd timer)

The live writer produces many small `part-*.parquet` files per day. Compaction merges them into one sorted file per event type, dramatically improving query performance. See `docs/why-compaction.md` for details.

### 1. Write the service unit

```bash
sudo tee /etc/systemd/system/kalshi-v2-compact.service > /dev/null <<'EOF'
[Unit]
Description=Compact yesterday's silver partitions
After=network.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/prediction-market-exploration
ExecStart=/bin/bash -c '/home/ubuntu/prediction-market-exploration/.venv/bin/python -m v2.scripts.compact_silver --date $(date -u -d yesterday +%%Y-%%m-%%d)'
StandardOutput=journal
StandardError=journal
EOF
```

### 2. Write the timer unit

```bash
sudo tee /etc/systemd/system/kalshi-v2-compact.timer > /dev/null <<'EOF'
[Unit]
Description=Daily silver compaction at 02:00 UTC

[Timer]
OnCalendar=*-*-* 07:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF
```

- **`Type=oneshot`** — runs to completion, then exits (not a long-running daemon).
- **`Persistent=true`** — if the machine was off at 02:00 UTC, the timer fires on next boot.
- **`OnCalendar=07:00 UTC`** — 2:00 AM EST (UTC-5), runs well after midnight so all of yesterday's data is flushed.

### 3. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-v2-compact.timer
```

### 4. Verify

```bash
# Confirm timer is scheduled
systemctl list-timers kalshi-v2-compact.timer

# Manual trigger to test
sudo systemctl start kalshi-v2-compact.service

# Check output
journalctl -u kalshi-v2-compact -e
```

### Operations cheatsheet (compaction)

```bash
# Check next scheduled run
systemctl list-timers kalshi-v2-compact.timer

# View compaction logs
journalctl -u kalshi-v2-compact --since "1 hour ago"
journalctl -u kalshi-v2-compact -e

# Manual run for a specific date
cd ~/prediction-market-exploration
.venv/bin/python -m v2.scripts.compact_silver --date 2026-04-30 --dry-run
.venv/bin/python -m v2.scripts.compact_silver --date 2026-04-30

# Disable timer
sudo systemctl disable --now kalshi-v2-compact.timer
```
