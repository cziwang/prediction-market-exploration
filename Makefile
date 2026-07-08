# Dependency setup for the pm pipeline.
#
#   make            — everything: venv (pm + dev tools + PyFlink), connector jar
#   make install    — venv (.venv); requires Python 3.11 (PyFlink supports <= 3.12)
#   make jars       — Flink Kafka connector jar (jars/ is gitignored)
#   make up / down  — Kafka + Flink via docker compose
#   make test / lint / typecheck / clean

EC2_ID  := i-0d970058962409dbf
EC2_KEY := ~/.ssh/ec2-prediction-market.pem
EC2_IP  := $(shell aws ec2 describe-instances --instance-ids $(EC2_ID) \
             --query 'Reservations[].Instances[].PublicIpAddress' \
             --output text 2>/dev/null)
EC2_SSH := ssh -i $(EC2_KEY) ubuntu@$(EC2_IP)

PYTHON ?= python3.11
VENV   := .venv
PIP    := $(VENV)/bin/pip

CONNECTOR_JAR := jars/flink-sql-connector-kafka-3.4.0-1.20.jar
CONNECTOR_URL := https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.4.0-1.20/flink-sql-connector-kafka-3.4.0-1.20.jar

.PHONY: all install jars up down submit-enrich test lint typecheck clean \
        ec2-start ec2-ip ec2-ssh ec2-bootstrap ec2-setup

all: install jars

# ── Venv: pm package + dev tools (pytest, mypy, ruff) + PyFlink ─────────

install: $(VENV)/.installed

$(VENV)/.installed: pyproject.toml
	@command -v $(PYTHON) >/dev/null || \
		{ echo "error: $(PYTHON) not found (brew install python@3.11)"; exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev,flink]"
	touch $@

# ── Flink Kafka connector jar ───────────────────────────────────────────

jars: $(CONNECTOR_JAR)

$(CONNECTOR_JAR):
	mkdir -p jars
	curl -fL -o $@ $(CONNECTOR_URL)

# ── Infra ────────────────────────────────────────────────────────────────

up:
	docker compose up -d

down:
	docker compose down

# ── Flink cluster job submission ─────────────────────────────────────────
# Submits from INSIDE the compose network (kafka:29092; repo mounted at
# /opt/pm). Web UI: http://localhost:8081

ENRICH_ARGS ?= --bootstrap-servers kafka:29092 \
               --game-map-file /opt/pm/reference/game_map.json \
               --parallelism 4 \
               --checkpoint-dir file:///flink-checkpoints

submit-enrich:
	docker compose exec flink-jobmanager flink run \
	  -py /opt/pm/src/pm/enrich/job.py $(ENRICH_ARGS)

# ── Dev loop ─────────────────────────────────────────────────────────────

test: install
	$(VENV)/bin/pytest

lint: install
	$(VENV)/bin/ruff check src tests

typecheck: install
	$(VENV)/bin/mypy

clean:
	rm -rf $(VENV) jars

# ── EC2 (run from Mac) ───────────────────────────────────────────────────
# Typical first-time sequence:
#   make ec2-start     # start the instance (no-op if already running)
#   make ec2-ip        # confirm IP + instance type
#   make ec2-bootstrap # one-time: grow disk, install docker/python, clone, start stack
#   make ec2-ssh       # open a shell
#
# After a stop/start (IP changes):
#   make ec2-ip && make ec2-ssh

ec2-start:
	aws ec2 start-instances --instance-ids $(EC2_ID) \
	  --query 'StartingInstances[].CurrentState.Name' --output text

ec2-ip:
	@aws ec2 describe-instances --instance-ids $(EC2_ID) \
	  --query 'Reservations[].Instances[].[InstanceType,PublicIpAddress]' \
	  --output table

ec2-ssh:
	@test -n "$(EC2_IP)" || { echo "error: instance has no public IP (is it running?)"; exit 1; }
	$(EC2_SSH)

# Copies the bootstrap script to EC2 and runs it.
# Safe to re-run after a docker group change (second run skips already-done steps).
ec2-bootstrap:
	@test -n "$(EC2_IP)" || { echo "error: instance has no public IP — run 'make ec2-start' first"; exit 1; }
	scp -i $(EC2_KEY) scripts/ec2_bootstrap.sh ubuntu@$(EC2_IP):/tmp/ec2_bootstrap.sh
	$(EC2_SSH) "bash /tmp/ec2_bootstrap.sh"

# Alias for re-running bootstrap after the mandatory docker group re-login
ec2-setup: ec2-bootstrap
