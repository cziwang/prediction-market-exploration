# Running `scripts/live/kalshi_ws` as a systemd service

Deployment guide for the Kalshi WebSocket bronze ingester
(`scripts/live/kalshi_ws/__main__.py`). Design lives in
[`data-flow.md`](data-flow.md); the NBA-side counterpart is
[`live-nba-cdn-service.md`](live-nba-cdn-service.md).

The service writes directly to
`s3://prediction-markets-data/bronze/kalshi_ws/` via `BronzeWriter` —
one S3 prefix per server-side message `type`
(`orderbook_snapshot`, `orderbook_delta`, `subscribed`, `error`, ...).

## Prerequisites

Assumes the EC2 instance is already set up per
[`live-pbp-ingestion.md`](live-pbp-ingestion.md) (Python 3.12 venv,
`requirements.txt` installed, IAM role with S3 access attached).

Unlike the NBA service, **this one needs Kalshi credentials on disk** —
Kalshi requires an authenticated connection even for public market
channels. Two secrets are involved:

- **`KALSHI_API_KEY_ID`** — the UUID-shaped key ID shown in Kalshi's
  API-keys UI.
- **`KALSHI_PRIVATE_KEY_PATH`** — absolute path to the RSA `.pem`
  private key file that Kalshi forces you to download when you create
  the key. Kalshi only shows you this file once, so save it.

Place the `.pem` outside any git-tracked directory (repo's `keys/` is
gitignored; `~/.kalshi/` also works) and lock it down:

```bash
mkdir -p ~/prediction-market-exploration/keys
# copy the .pem in by whatever means (scp, cat-paste, etc.)
chmod 600 ~/prediction-market-exploration/keys/kalshi-private-key.pem
```

Then append to `~/prediction-market-exploration/.env`:

```
KALSHI_API_KEY_ID=<uuid from kalshi>
KALSHI_PRIVATE_KEY_PATH=/home/ubuntu/prediction-market-exploration/keys/kalshi-private-key.pem
```

Use an absolute path for `KALSHI_PRIVATE_KEY_PATH` — it survives any
`cd` confusion and matches the systemd `WorkingDirectory=` without
assumptions. Then:

```bash
chmod 600 ~/prediction-market-exploration/.env
```

Pull the latest code and install the two deps the Kalshi path adds on
top of the NBA path (`websockets`, `cryptography`):

```bash
cd ~/prediction-market-exploration
git pull
.venv/bin/pip install -r requirements.txt
```

Foreground smoke test before making it a service. Expect to see
`subscribing ['orderbook_delta'] to N markets` within a second or two,
then a stream of `bronze flush` lines every ~60 s:

```bash
.venv/bin/python -m scripts.live.kalshi_ws
# Ctrl-C to stop; watch it drain buffers to S3 before exiting.
```

## 1. Write the unit file

```bash
sudo tee /etc/systemd/system/kalshi-live.service > /dev/null <<'EOF'
[Unit]
Description=Kalshi Live Bronze Ingester (WebSocket → S3 bronze)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/prediction-market-exploration
ExecStart=/home/ubuntu/prediction-market-exploration/.venv/bin/python -m scripts.live.kalshi_ws
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

Identical shape to `nba-live.service`:

- `KillSignal=SIGINT` — matches the interactive Ctrl-C path; the
  ingester's signal handler closes the WS cleanly and
  `async with BronzeWriter` drains buffers.
- `TimeoutStopSec=30` — give `BronzeWriter` up to 30 s to flush before
  systemd escalates to SIGKILL.
- `Restart=always` + `RestartSec=10` — auto-recover from crashes or
  Kalshi-side connection resets. The ingester itself already
  reconnects with exponential backoff, so systemd restarts are the
  second line of defense.

## 2. Register and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable kalshi-live
sudo systemctl start kalshi-live
```

## 3. Verify

```bash
# Should show "Active: active (running)"
sudo systemctl status kalshi-live

# Follow logs
journalctl -u kalshi-live -f

# Bronze files should appear within ~60s. Expect prefixes for:
#   orderbook_snapshot/  — one record per subscribed market, once per reconnect
#   orderbook_delta/     — the continuous stream
#   subscribed/          — a small ack file each subscribe cycle
aws s3 ls s3://prediction-markets-data/bronze/kalshi_ws/ --recursive | tail
```

During quiet hours you should still see dozens of delta records per
second even without any live games, because KXNBAGAME markets trade
around upcoming games too. During a live game, delta volume climbs
significantly.

## Operations cheatsheet

```bash
# Status / logs
sudo systemctl status kalshi-live
journalctl -u kalshi-live -f
journalctl -u kalshi-live --since "1 hour ago"

# Restart after pulling new code
cd ~/prediction-market-exploration && git pull
sudo systemctl restart kalshi-live

# Stop / start without changing boot behavior
sudo systemctl stop kalshi-live
sudo systemctl start kalshi-live

# Disable auto-start on boot (unit file remains on disk)
sudo systemctl disable kalshi-live

# Rotate Kalshi credentials
# 1. Create a new key in Kalshi's API-keys UI, download the new .pem
# 2. Replace the .pem on disk (chmod 600), update KALSHI_API_KEY_ID in .env
# 3. sudo systemctl restart kalshi-live
# 4. Delete the old key in Kalshi's UI
```

## Operational notes

### Reconnect behavior

The ingester reconnects on its own with exponential backoff
(`RECONNECT_INITIAL=1s`, capped at `RECONNECT_MAX=60s`). Reconnect
triggers a fresh REST lookup of open KXNBAGAME markets and a new
subscribe — which means a new `orderbook_snapshot` per market, which
is what you want: the book is authoritative only after the most recent
snapshot.

If KXNBAGAME has no open markets (off-season, deep off-hours),
`_connect_once()` returns without opening a connection and the loop
idles for `NO_MARKETS_BACKOFF=300s` before re-checking. This keeps the
process alive through the off-season without thrashing.

### Scope

v1 covers **only** the `orderbook_delta` channel on **only** the
`KXNBAGAME` series, per `data-flow.md` open question #5. Expanding to
other series (`KXNBASPREAD`, `KXNBATOTAL`, player props) or other
channels (`trade`, `ticker`, `market_lifecycle_v2`) is a matter of
editing `CHANNELS` and `SERIES_TICKER` in `scripts/live/kalshi_ws/__main__.py`
and bouncing the service.

### Crash-recovery caveat

Same as `nba-live`: `BronzeWriter` buffers in memory and flushes every
60 s or 5 MB. A crash loses up to one flush window — roughly one
minute of records across all channels. There is no on-disk staging.

Kalshi's own protocol helps here: on reconnect we get a fresh
`orderbook_snapshot` per market, so the book state is consistent again
even if some deltas were lost during the crash window. Missed deltas
are genuinely gone from bronze, not just from the live strategy's
view.

Accepted tradeoff per `data-flow.md`. If bronze durability matters
more, revisit by lowering `flush_seconds` on `BronzeWriter` (at the
cost of more, smaller S3 objects) or by staging to local disk first.

### Secrets hygiene

`.env` and `keys/` are gitignored. Never commit either. If you
accidentally do, rotate the Kalshi key immediately and rewrite git
history. Kalshi keys are revoked from the API-keys UI.
