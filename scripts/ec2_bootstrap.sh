#!/usr/bin/env bash
# One-time EC2 setup. Run via: make ec2-bootstrap
# Safe to re-run: each step is idempotent.
set -euo pipefail

# ── 1. Grow filesystem into resized EBS volume ────────────────────────────
ROOT_DEV=$(lsblk -rno NAME,MOUNTPOINT | awk '$2=="/" {print $1}')
DISK="/dev/$(echo "$ROOT_DEV" | sed 's/p[0-9]*$//')"
PART_NUM=$(echo "$ROOT_DEV" | grep -o '[0-9]*$')
echo "==> Growing $DISK partition $PART_NUM..."
sudo growpart "$DISK" "$PART_NUM" || true   # exits 1 if already at full size
sudo resize2fs "/dev/$ROOT_DEV" || true
df -h /

# ── 2. Docker ─────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "==> Installing docker..."
    sudo apt-get update -y
    sudo apt-get install -y docker.io docker-compose-v2
fi
if ! groups ubuntu | grep -q docker; then
    echo "==> Adding ubuntu to docker group (re-login required)..."
    sudo usermod -aG docker ubuntu
    echo "IMPORTANT: log out and back in, then re-run: make ec2-setup"
    exit 0
fi

# ── 3. Python 3.11 ───────────────────────────────────────────────────────
if ! command -v python3.11 &>/dev/null; then
    echo "==> Installing python3.11..."
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get install -y python3.11 python3.11-venv
fi

# ── 4. Clone / update repo ───────────────────────────────────────────────
REPO_DIR="$HOME/prediction-market-exploration"
if [ -d "$REPO_DIR/.git" ]; then
    echo "==> Pulling latest..."
    git -C "$REPO_DIR" pull
else
    echo "==> Cloning..."
    rm -rf "$REPO_DIR"
    git clone https://github.com/cziwang/prediction-market-exploration.git "$REPO_DIR"
fi

# ── 5. Build venv + jars + docker images ─────────────────────────────────
cd "$REPO_DIR"
make
docker compose build
docker compose up -d
docker compose ps

echo ""
echo "==> Bootstrap complete. Stack is up."
