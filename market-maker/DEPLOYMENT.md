# Deployment

## Overview

Two services on a single EC2 instance + one batch job via cron:

| Component | What it does | Runs on | Schedule |
|-----------|-------------|---------|----------|
| **Collector** | WS deltas/trades + REST snapshots → S3 bronze | EC2 systemd service | Continuous |
| **Rebuild** | Bronze → silver LOB reconstruction | EC2 cron job | Daily, after midnight UTC |
| **Query** | Ad-hoc SQL over silver Parquet | Athena | On-demand |

## Infrastructure

### EC2 Instance

- **Type:** `t3.small` (2 vCPU, 2 GB) — sufficient for collector + daily rebuild
- **AMI:** Amazon Linux 2023 or Ubuntu 24.04
- **Storage:** 20 GB gp3 (code + venv + temp buffers)
- **IAM instance profile** with S3 permissions (see below)
- Public subnet with auto-assign public IP (needs outbound to Kalshi WS/REST and S3)

### S3

Bucket: `prediction-markets-data`

```
mm/
├── bronze/
│   ├── deltas/dt=2026-05-03/*.parquet
│   ├── snapshots/dt=2026-05-03/*.parquet
│   └── trades/dt=2026-05-03/*.parquet
└── silver/
    └── lob/dt=2026-05-03/lob.parquet
```

Lifecycle policy: transition bronze to S3-IA after 30 days, Glacier after 90. Silver stays in Standard (queried frequently).

### IAM Instance Profile

Single role attached to the EC2 instance:

```json
{
  "Effect": "Allow",
  "Action": ["s3:PutObject"],
  "Resource": "arn:aws:s3:::prediction-markets-data/mm/bronze/*"
},
{
  "Effect": "Allow",
  "Action": ["s3:GetObject"],
  "Resource": "arn:aws:s3:::prediction-markets-data/mm/bronze/*"
},
{
  "Effect": "Allow",
  "Action": ["s3:PutObject"],
  "Resource": "arn:aws:s3:::prediction-markets-data/mm/silver/*"
},
{
  "Effect": "Allow",
  "Action": ["s3:ListBucket"],
  "Resource": "arn:aws:s3:::prediction-markets-data"
}
```

## EC2 Setup

### 1. Install dependencies

```bash
sudo dnf install -y python3.12 python3.12-pip git  # Amazon Linux 2023
# or: sudo apt install -y python3.12 python3.12-venv git  # Ubuntu

git clone <repo-url> /home/ec2-user/prediction-market-exploration
cd /home/ec2-user/prediction-market-exploration
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r market-maker/requirements.txt
```

### 2. Secrets

Place credentials directly on the instance:

```bash
# .env in repo root
cat > /home/ec2-user/prediction-market-exploration/market-maker/.env <<'EOF'
KALSHI_API_KEY_ID=<your-key-id>
KALSHI_PRIVATE_KEY_PATH=/home/ec2-user/.kalshi/private_key.pem
EOF

# RSA private key
mkdir -p /home/ec2-user/.kalshi
# Copy your PEM file here
chmod 600 /home/ec2-user/.kalshi/private_key.pem
```

### 3. systemd — Collector

Create `/etc/systemd/system/mm-collector.service`:

```ini
[Unit]
Description=Kalshi market data collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/prediction-market-exploration
ExecStart=/home/ec2-user/prediction-market-exploration/.venv/bin/python -m market-maker.scripts.collect --prod
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable mm-collector
sudo systemctl start mm-collector
```

Useful commands:

```bash
sudo systemctl status mm-collector    # check status
journalctl -u mm-collector -f         # tail logs
journalctl -u mm-collector --since "1 hour ago"  # recent logs
sudo systemctl restart mm-collector   # restart after code changes
```

### 4. Cron — Daily Rebuild

Add to ec2-user's crontab (`crontab -e`):

```cron
# Reconstruct previous day's silver LOB at 01:00 UTC
0 1 * * * cd /home/ec2-user/prediction-market-exploration && .venv/bin/python -m market-maker.scripts.rebuild $(date -u -d 'yesterday' +\%Y-\%m-\%d) >> /var/log/mm-rebuild.log 2>&1
```

### Athena — Query layer

No infra to manage. Create a Glue Catalog database + tables pointing at S3.

**LOB table DDL:**
```sql
CREATE EXTERNAL TABLE mm_lob (
    market_ticker STRING,
    ts            BIGINT,
    side          STRING,
    price_cents   INT,
    qty           INT
)
PARTITIONED BY (dt STRING)
STORED AS PARQUET
LOCATION 's3://prediction-markets-data/mm/silver/lob/'
TBLPROPERTIES ('parquet.compression'='ZSTD');
```

**Trades table DDL:**
```sql
CREATE EXTERNAL TABLE mm_trades (
    market_ticker    STRING,
    ts               BIGINT,
    yes_price_cents  INT,
    no_price_cents   INT,
    count            INT,
    taker_side       STRING
)
PARTITIONED BY (dt STRING)
STORED AS PARQUET
LOCATION 's3://prediction-markets-data/mm/bronze/trades/'
TBLPROPERTIES ('parquet.compression'='ZSTD');
```

After each rebuild, run `MSCK REPAIR TABLE mm_lob;` to pick up new partitions.

## Deploy workflow

```bash
# On EC2 instance:
cd /home/ec2-user/prediction-market-exploration
git pull
source .venv/bin/activate
pip install -r market-maker/requirements.txt  # if deps changed
sudo systemctl restart mm-collector
```

## Monitoring

| Signal | Method | Alert threshold |
|--------|--------|-----------------|
| Collector alive | `journalctl -u mm-collector` / CloudWatch agent | No `Flushed` in 5 min |
| WS reconnects | `journalctl` grep for `reconnecting` | >5 in 10 min |
| Bronze file count | S3 metrics or custom metric | 0 files written in 1 hour |
| Rebuild success | Check cron log `/var/log/mm-rebuild.log` | Any error |
| Drift detected | `journalctl` grep for `DRIFT` | Any occurrence |
| Instance health | EC2 status checks | Auto-recover enabled |

Optional: install the CloudWatch agent to ship `journalctl` logs to CloudWatch for remote alerting.

## Cost estimate (rough)

| Component | Monthly cost |
|-----------|-------------|
| EC2 t3.small (on-demand, 24/7) | ~$15 |
| EC2 t3.small (1yr reserved) | ~$9 |
| S3 storage (bronze ~5 GB/day compressed) | ~$35/mo at Standard |
| S3 requests (PutObject) | ~$5 |
| Athena queries | $5/TB scanned, negligible |
| **Total** | **~$55/mo** (on-demand) |

## Rollout order

1. **Launch EC2 instance** with IAM profile, install deps, clone repo
2. **Place secrets** (`.env` + PEM file)
3. **Start collector on demo** — remove `--prod` from service file, verify bronze lands in S3
4. **Switch to prod** — add `--prod` back, `systemctl restart`, monitor 24h
5. **Set up cron rebuild** — run manually for first day, verify silver output
6. **Create Athena tables** — run DDL, verify queries work
7. **Set up monitoring** — CloudWatch agent or just SSH + journalctl
