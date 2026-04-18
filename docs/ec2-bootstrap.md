# EC2 bootstrap for live ingesters

One-time setup for the EC2 instance that hosts the live ingesters
(`scripts/live/nba_cdn/`, `scripts/live/kalshi_ws/`). Covers the
instance itself, OS-level dependencies, Python venv, and AWS CLI.
Service-specific install steps (systemd unit files, credentials) live
in each service's own doc:

- [`live-nba-cdn-service.md`](live-nba-cdn-service.md) — NBA CDN ingester
- [`live-kalshi-ws-service.md`](live-kalshi-ws-service.md) — Kalshi WS ingester

This section assumes no prior Linux or AWS experience. EC2 is AWS's
service for renting virtual Linux machines; once launched, an EC2
instance behaves like a regular computer you SSH into.

## Instance settings

Settings to choose when launching the instance in the AWS console.
These optimize for cost and let the ingesters upload to S3 without
storing AWS keys on disk.

- **Instance type: `t3.micro` or `t4g.micro`** — the machine's
  CPU/memory tier. `t3` is x86_64, `t4g` is ARM (slightly cheaper).
  Either works; pick one and be consistent with the AMI architecture
  below. ~$3/mo with free tier, ~$8/mo without. Plenty for polling a
  handful of HTTP endpoints every few seconds and holding one Kalshi
  WebSocket. *Current deployment: `t3.micro` (x86_64).*
- **Region: `us-east-1`** — which AWS data center hosts the machine.
  NBA's CDN is fronted by CloudFront with edge nodes near Virginia,
  and Kalshi's API is also east-coast-hosted, so `us-east-1` gets
  10–30 ms RTT to both.
- **AMI: Ubuntu 24.04 LTS** — the OS image the machine boots from.
  Match the architecture to the instance type (x86_64 AMI for `t3`,
  ARM AMI for `t4g`).
- **Storage: 8 GB gp3** — disk size. The live ingesters don't write
  to disk beyond logs and the occasional `.pem` for Kalshi; 8 GB is
  ample.
- **Security group: SSH inbound only** — the virtual firewall. You
  only need port 22 (SSH) open to log in; the ingesters make outbound
  calls only.
- **IAM instance profile** — AWS credentials attached to the
  instance itself, instead of a file. Create an IAM role (trusted
  entity: **EC2**) with these AWS-managed policies attached:
  `AmazonS3FullAccess` (for reading/writing raw data to the
  `prediction-markets-data` bucket) and `AmazonSSMReadOnlyAccess`
  (reserved for future credentials in Parameter Store). Attach the
  role to the instance via EC2 → Instances → Actions → Security →
  Modify IAM role. The ingesters then hit S3 automatically with no
  keys on disk. *Current deployment: role `prediction-markets-poller`.*
  These managed policies are broader than strictly necessary; scope
  down with a custom policy (`s3:PutObject` / `s3:GetObject` /
  `s3:ListBucket` on the one bucket, `ssm:GetParameter` on
  `/prediction-market/*`) when hardening later.

## First-time setup

**SSH in.** SSH ("secure shell") opens a terminal on a remote
machine. The `.pem` file is the private key AWS generated when you
created the instance; `ubuntu` is the default username on Ubuntu
AMIs. The instance's public IP is shown in the EC2 console.

```bash
ssh -i ~/.ssh/ec2-prediction-market.pem ubuntu@<instance-ip>
```

Once connected, every command below runs **on the instance**, not
your laptop.

```bash
# Refresh the list of available packages, then install Python 3.12's
# venv module and git. apt is Ubuntu's package manager; sudo runs
# the command as root (required to install system software);
# -y auto-confirms the install prompt.
sudo apt update && sudo apt install -y python3.12-venv git

# Download the project source code into a folder in your home
# directory (~).
git clone <your-repo-url> ~/prediction-market-exploration
cd ~/prediction-market-exploration

# Create an isolated Python environment in .venv/ so project
# dependencies don't collide with system Python or other projects.
python3.12 -m venv .venv

# Install the project's Python dependencies into that venv.
.venv/bin/pip install -r requirements.txt
```

## Install the AWS CLI and verify the IAM role

The AWS CLI is the command-line tool for talking to AWS. Here we use
it only to sanity-check that the IAM role is attached (otherwise S3
writes will fail silently from the ingesters). Ubuntu 24.04 no longer
ships `awscli` via apt, so use snap — one command, auto-updates,
arch-agnostic:

```bash
sudo snap install aws-cli --classic

# Should print the IAM role's identity ARN, ending in
# /<role-name>/<instance-id>. "Unable to locate credentials" means
# the role isn't attached to the instance yet.
aws sts get-caller-identity

# Should list the top-level prefixes (kalshi/, nba/, nba_cdn/,
# bronze/). AccessDenied means the role's policy is missing S3 read
# permissions.
aws s3 ls s3://prediction-markets-data/
```

## Smoke test

Verify the writer path works end-to-end before installing any
service. This round-trips synthetic records through `BronzeWriter`
and `SilverWriter` under a throwaway `smoketest_*` S3 prefix:

```bash
.venv/bin/python -m scripts.infra.smoke_test
```

Expect `smoke test passed ✓` at the bottom. A failure here points at
AWS credentials or network, not ingester code.

## Next steps

Pick a service to install:

- **NBA CDN ingester:** follow
  [`live-nba-cdn-service.md`](live-nba-cdn-service.md). No extra
  credentials needed — the instance's IAM role handles S3.
- **Kalshi WS ingester:** follow
  [`live-kalshi-ws-service.md`](live-kalshi-ws-service.md). Requires
  a Kalshi API key and RSA `.pem` private key on the instance before
  the service will start.
