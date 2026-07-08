# Dependency setup for the pm pipeline.
#
#   make            — everything: main venv, Flink venv, connector jar
#   make install    — main venv (.venv) with pm + dev tools
#   make flink      — PyFlink venv (.venv-flink); requires Python <= 3.12
#   make jars       — Flink Kafka connector jar (jars/ is gitignored)
#   make up / down  — Kafka + Flink via docker compose
#   make test / lint / typecheck / clean

PYTHON        ?= python3
FLINK_PYTHON  ?= python3.11
VENV          := .venv
FLINK_VENV    := .venv-flink
PIP           := $(VENV)/bin/pip
FLINK_PIP     := $(FLINK_VENV)/bin/pip

FLINK_VERSION   := 1.20.5
CONNECTOR_JAR   := jars/flink-sql-connector-kafka-3.4.0-1.20.jar
CONNECTOR_URL   := https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.4.0-1.20/flink-sql-connector-kafka-3.4.0-1.20.jar

.PHONY: all install flink jars up down test lint typecheck clean

all: install flink jars

# ── Main venv: pm package + dev tools (pytest, mypy, ruff) ──────────────

install: $(VENV)/.installed

$(VENV)/.installed: pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	touch $@

# ── Flink venv: PyFlink requires Python <= 3.12, hence the separate env ─

flink: $(FLINK_VENV)/.installed

$(FLINK_VENV)/.installed: pyproject.toml
	@command -v $(FLINK_PYTHON) >/dev/null || \
		{ echo "error: $(FLINK_PYTHON) not found (brew install python@3.11)"; exit 1; }
	$(FLINK_PYTHON) -m venv $(FLINK_VENV)
	$(FLINK_PIP) install --upgrade pip
	$(FLINK_PIP) install -e . apache-flink==$(FLINK_VERSION)
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

# ── Dev loop ─────────────────────────────────────────────────────────────

test: install
	$(VENV)/bin/pytest

lint: install
	$(VENV)/bin/ruff check src tests

typecheck: install
	$(VENV)/bin/mypy

clean:
	rm -rf $(VENV) $(FLINK_VENV) jars
