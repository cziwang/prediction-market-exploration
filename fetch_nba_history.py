"""Fetch settled NBA game markets from Kalshi and store them in SQLite.

Usage:
    python fetch_nba_history.py                 # markets only
    python fetch_nba_history.py --candles       # + 1-minute OHLC per market
    python fetch_nba_history.py --candles --period 60
    python fetch_nba_history.py --candles --refresh-candles

Output: ./data/kalshi_nba.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from kalshi_client import KalshiClient

SERIES = "KXNBAGAME"
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "kalshi_nba.db"

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


def iso_to_ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def init_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def upsert_markets(conn: sqlite3.Connection, client: KalshiClient) -> int:
    """Pull from both /historical/markets and /markets?status=settled and dedupe."""
    seen: set[str] = set()
    total = 0
    sources = [
        ("/historical/markets", {"series_ticker": SERIES, "limit": 1000}),
        ("/markets", {"series_ticker": SERIES, "status": "settled", "limit": 1000}),
    ]
    for path, params in sources:
        print(f"  → {path} {params}")
        page_count = 0
        for m in client.paginate(path, params, "markets"):
            t = m.get("ticker")
            if not t or t in seen:
                continue
            seen.add(t)
            conn.execute(
                """
                INSERT OR REPLACE INTO markets
                (ticker, event_ticker, series_ticker, title, yes_sub_title,
                 no_sub_title, open_time, close_time, status, result, volume, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t,
                    m.get("event_ticker"),
                    SERIES,
                    m.get("title"),
                    m.get("yes_sub_title"),
                    m.get("no_sub_title"),
                    m.get("open_time"),
                    m.get("close_time"),
                    m.get("status"),
                    m.get("result"),
                    m.get("volume"),
                    json.dumps(m),
                ),
            )
            total += 1
            page_count += 1
            if page_count % 500 == 0:
                conn.commit()
                print(f"    upserted {page_count} so far…")
        conn.commit()
    return total


def fetch_candlesticks(
    conn: sqlite3.Connection,
    client: KalshiClient,
    period_interval: int,
    skip_existing: bool,
    throttle_s: float,
) -> None:
    rows = conn.execute(
        """
        SELECT ticker, open_time, close_time
        FROM markets
        WHERE status = 'settled'
          AND open_time IS NOT NULL
          AND close_time IS NOT NULL
        ORDER BY close_time DESC
        """
    ).fetchall()

    n = len(rows)
    print(f"  {n} settled markets to consider")

    for i, (ticker, open_time, close_time) in enumerate(rows, 1):
        if skip_existing:
            hit = conn.execute(
                "SELECT 1 FROM fetch_log WHERE ticker = ? AND period_min = ?",
                (ticker, period_interval),
            ).fetchone()
            if hit:
                continue

        start_ts = iso_to_ts(open_time)
        end_ts = iso_to_ts(close_time)
        if start_ts is None or end_ts is None or end_ts <= start_ts:
            continue

        try:
            data = client.get(
                f"/series/{SERIES}/markets/{ticker}/candlesticks",
                {
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "period_interval": period_interval,
                },
            )
        except requests.HTTPError as e:
            print(f"  [{i}/{n}] {ticker}: HTTP {e.response.status_code if e.response else '?'}")
            continue
        except requests.RequestException as e:
            print(f"  [{i}/{n}] {ticker}: {e}")
            continue

        candles = data.get("candlesticks", []) or []
        for c in candles:
            price = c.get("price") or {}
            yes_bid = c.get("yes_bid") or {}
            yes_ask = c.get("yes_ask") or {}
            conn.execute(
                """
                INSERT OR REPLACE INTO candlesticks
                (ticker, end_period_ts, price_open, price_high, price_low,
                 price_close, volume, open_interest, yes_bid_close, yes_ask_close)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    c.get("end_period_ts"),
                    price.get("open"),
                    price.get("high"),
                    price.get("low"),
                    price.get("close"),
                    c.get("volume", c.get("volume_fp")),
                    c.get("open_interest", c.get("open_interest_fp")),
                    yes_bid.get("close"),
                    yes_ask.get("close"),
                ),
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO fetch_log (ticker, fetched_at, candle_count, period_min)
            VALUES (?, ?, ?, ?)
            """,
            (
                ticker,
                datetime.now(timezone.utc).isoformat(),
                len(candles),
                period_interval,
            ),
        )
        conn.commit()
        print(f"  [{i}/{n}] {ticker}: {len(candles)} candles")
        if throttle_s:
            time.sleep(throttle_s)


def summarize(conn: sqlite3.Connection) -> None:
    m_total = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    m_settled = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE status = 'settled'"
    ).fetchone()[0]
    c_total = conn.execute("SELECT COUNT(*) FROM candlesticks").fetchone()[0]
    c_tickers = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM candlesticks"
    ).fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(close_time), MAX(close_time) FROM markets WHERE status = 'settled'"
    ).fetchone()
    print()
    print(f"Markets:      {m_total} total, {m_settled} settled")
    print(f"Candles:      {c_total} rows across {c_tickers} markets")
    if date_range[0]:
        print(f"Date range:   {date_range[0]} → {date_range[1]}")
    print(f"DB:           {DB_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--candles",
        action="store_true",
        help="Also fetch OHLC candlesticks for each settled market",
    )
    ap.add_argument(
        "--period",
        type=int,
        default=1,
        choices=[1, 60, 1440],
        help="Candlestick interval in minutes (default: 1)",
    )
    ap.add_argument(
        "--refresh-candles",
        action="store_true",
        help="Re-fetch candlesticks even if already stored for this period",
    )
    ap.add_argument(
        "--throttle",
        type=float,
        default=0.1,
        help="Seconds to sleep between candlestick requests (default: 0.1)",
    )
    ap.add_argument(
        "--skip-markets",
        action="store_true",
        help="Skip refreshing market metadata (only fetch candles)",
    )
    args = ap.parse_args()

    client = KalshiClient()
    print(f"Kalshi client authenticated: {client.authenticated}")

    conn = init_db()

    if not args.skip_markets:
        print(f"\nFetching markets for series {SERIES}…")
        n = upsert_markets(conn, client)
        print(f"  upserted {n} unique markets")

    if args.candles:
        print(f"\nFetching {args.period}-minute candlesticks…")
        fetch_candlesticks(
            conn,
            client,
            period_interval=args.period,
            skip_existing=not args.refresh_candles,
            throttle_s=args.throttle,
        )

    summarize(conn)


if __name__ == "__main__":
    main()
