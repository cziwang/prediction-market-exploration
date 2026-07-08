# Dependency setup for the pm pipeline.
#
#   make            — everything: venv (pm + dev tools + PyFlink), connector jar
#   make install    — venv (.venv); requires Python 3.11 (PyFlink supports <= 3.12)
#   make jars       — Flink Kafka connector jar (jars/ is gitignored)
#   make up / down  — Kafka + Flink via docker compose
#   make test / lint / typecheck / clean

PYTHON ?= python3.11
VENV   := .venv
PIP    := $(VENV)/bin/pip

CONNECTOR_JAR := jars/flink-sql-connector-kafka-3.4.0-1.20.jar
CONNECTOR_URL := https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.4.0-1.20/flink-sql-connector-kafka-3.4.0-1.20.jar

.PHONY: all install jars up down submit-enrich test lint typecheck clean

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
