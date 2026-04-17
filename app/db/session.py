import sqlite3
from typing import Any

from app.core.config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker         TEXT PRIMARY KEY,
    event_ticker   TEXT,
    series_ticker  TEXT,
    title          TEXT,
    yes_sub_title  TEXT,
    no_sub_title   TEXT,
    open_time      TEXT,
    close_time     TEXT,
    status         TEXT,
    result         TEXT,
    volume         INTEGER,
    raw_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_close ON markets(close_time);

CREATE TABLE IF NOT EXISTS candlesticks (
    ticker           TEXT,
    end_period_ts    INTEGER,
    price_open       REAL,
    price_high       REAL,
    price_low        REAL,
    price_close      REAL,
    volume           INTEGER,
    open_interest    INTEGER,
    yes_bid_close    REAL,
    yes_ask_close    REAL,
    PRIMARY KEY (ticker, end_period_ts)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    ticker       TEXT PRIMARY KEY,
    fetched_at   TEXT,
    candle_count INTEGER,
    period_min   INTEGER
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
