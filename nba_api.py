"""NBA Prediction Market API.

Serves Kalshi NBA game market data and candlesticks from the local SQLite DB.

    uvicorn nba_api:app --reload
    # Docs: http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

DB_PATH = Path(__file__).resolve().parent / "data" / "kalshi_nba.db"

app = FastAPI(
    title="NBA Prediction Market API",
    description="Query Kalshi NBA game markets and OHLC candlestick data.",
    version="0.1.0",
)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


# ── Markets ──────────────────────────────────────────────────────────────────


@app.get("/markets", tags=["Markets"])
def list_markets(
    status: str | None = Query(None, description="Filter by status (e.g. finalized, settled)"),
    result: str | None = Query(None, description="Filter by result (yes / no)"),
    event_ticker: str | None = Query(None, description="Filter by event_ticker"),
    search: str | None = Query(None, description="Search title (case-insensitive substring)"),
    sort: str = Query("close_time", description="Sort column"),
    order: str = Query("desc", description="asc or desc"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """List markets with optional filters, search, and pagination."""
    allowed_sorts = {"close_time", "open_time", "volume", "ticker", "title"}
    if sort not in allowed_sorts:
        raise HTTPException(400, f"sort must be one of {allowed_sorts}")
    if order not in ("asc", "desc"):
        raise HTTPException(400, "order must be asc or desc")

    clauses: list[str] = []
    params: list[Any] = []

    if status:
        clauses.append("status = ?")
        params.append(status)
    if result:
        clauses.append("result = ?")
        params.append(result)
    if event_ticker:
        clauses.append("event_ticker = ?")
        params.append(event_ticker)
    if search:
        clauses.append("title LIKE ?")
        params.append(f"%{search}%")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    conn = get_db()
    total = conn.execute(
        f"SELECT COUNT(*) FROM markets {where}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"""SELECT ticker, event_ticker, series_ticker, title, yes_sub_title,
                   no_sub_title, open_time, close_time, status, result, volume
            FROM markets {where}
            ORDER BY {sort} {order}
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    conn.close()

    return {"total": total, "limit": limit, "offset": offset, "markets": rows_to_dicts(rows)}


@app.get("/markets/{ticker}", tags=["Markets"])
def get_market(ticker: str) -> dict:
    """Get a single market by ticker."""
    conn = get_db()
    row = conn.execute(
        """SELECT ticker, event_ticker, series_ticker, title, yes_sub_title,
                  no_sub_title, open_time, close_time, status, result, volume
           FROM markets WHERE ticker = ?""",
        (ticker,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Market not found")
    return dict(row)


# ── Events (grouped markets) ────────────────────────────────────────────────


@app.get("/events", tags=["Events"])
def list_events(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """List events (each NBA game produces one event with two side-markets)."""
    conn = get_db()
    total = conn.execute("SELECT COUNT(DISTINCT event_ticker) FROM markets").fetchone()[0]
    rows = conn.execute(
        """SELECT event_ticker,
                  MIN(title) AS title,
                  MIN(open_time) AS open_time,
                  MAX(close_time) AS close_time,
                  SUM(volume) AS total_volume,
                  COUNT(*) AS market_count,
                  GROUP_CONCAT(ticker) AS tickers
           FROM markets
           GROUP BY event_ticker
           ORDER BY close_time DESC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    conn.close()
    return {"total": total, "limit": limit, "offset": offset, "events": rows_to_dicts(rows)}


@app.get("/events/{event_ticker}", tags=["Events"])
def get_event(event_ticker: str) -> dict:
    """Get all markets for a single event."""
    conn = get_db()
    rows = conn.execute(
        """SELECT ticker, event_ticker, series_ticker, title, yes_sub_title,
                  no_sub_title, open_time, close_time, status, result, volume
           FROM markets WHERE event_ticker = ?""",
        (event_ticker,),
    ).fetchall()
    conn.close()
    if not rows:
        raise HTTPException(404, "Event not found")
    return {"event_ticker": event_ticker, "markets": rows_to_dicts(rows)}


# ── Candlesticks ─────────────────────────────────────────────────────────────


@app.get("/markets/{ticker}/candlesticks", tags=["Candlesticks"])
def get_candlesticks(
    ticker: str,
    start_ts: int | None = Query(None, description="Unix timestamp lower bound"),
    end_ts: int | None = Query(None, description="Unix timestamp upper bound"),
    limit: int = Query(5000, ge=1, le=50000),
) -> dict:
    """Get OHLC candlestick data for a market."""
    clauses = ["ticker = ?"]
    params: list[Any] = [ticker]

    if start_ts is not None:
        clauses.append("end_period_ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("end_period_ts <= ?")
        params.append(end_ts)

    where = "WHERE " + " AND ".join(clauses)

    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) FROM candlesticks {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT end_period_ts, price_open, price_high, price_low, price_close,
                   volume, open_interest, yes_bid_close, yes_ask_close
            FROM candlesticks {where}
            ORDER BY end_period_ts ASC
            LIMIT ?""",
        params + [limit],
    ).fetchall()
    conn.close()
    return {"ticker": ticker, "total": total, "count": len(rows), "candlesticks": rows_to_dicts(rows)}


# ── Analytics ────────────────────────────────────────────────────────────────


@app.get("/analytics/summary", tags=["Analytics"])
def summary() -> dict:
    """High-level summary stats for the dataset."""
    conn = get_db()
    m_total = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    m_settled = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE result IS NOT NULL AND result != ''"
    ).fetchone()[0]
    c_total = conn.execute("SELECT COUNT(*) FROM candlesticks").fetchone()[0]
    c_tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM candlesticks").fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(close_time), MAX(close_time) FROM markets"
    ).fetchone()
    vol = conn.execute("SELECT SUM(volume) FROM markets").fetchone()[0]
    conn.close()
    return {
        "markets_total": m_total,
        "markets_settled": m_settled,
        "candlestick_rows": c_total,
        "candlestick_tickers": c_tickers,
        "total_volume": vol,
        "earliest_close": date_range[0],
        "latest_close": date_range[1],
    }


@app.get("/analytics/biggest-swings", tags=["Analytics"])
def biggest_swings(
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Markets with the largest intra-game price swings."""
    conn = get_db()
    rows = conn.execute(
        """SELECT c.ticker,
                  m.title,
                  m.result,
                  MAX(c.price_high) - MIN(c.price_low) AS swing_cents,
                  MIN(c.price_low) AS min_price,
                  MAX(c.price_high) AS max_price,
                  COUNT(*) AS candle_count
           FROM candlesticks c
           JOIN markets m ON m.ticker = c.ticker
           WHERE c.price_high IS NOT NULL AND c.price_low IS NOT NULL
           GROUP BY c.ticker
           ORDER BY swing_cents DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return {"swings": rows_to_dicts(rows)}


@app.get("/analytics/volume-leaders", tags=["Analytics"])
def volume_leaders(
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Markets ranked by trading volume."""
    conn = get_db()
    rows = conn.execute(
        """SELECT ticker, title, result, volume, open_time, close_time
           FROM markets
           ORDER BY volume DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return {"markets": rows_to_dicts(rows)}


@app.get("/analytics/result-distribution", tags=["Analytics"])
def result_distribution() -> dict:
    """Count of yes vs no outcomes across all settled markets."""
    conn = get_db()
    rows = conn.execute(
        """SELECT result, COUNT(*) AS count, SUM(volume) AS total_volume
           FROM markets
           WHERE result IS NOT NULL AND result != ''
           GROUP BY result"""
    ).fetchall()
    conn.close()
    return {"distribution": rows_to_dicts(rows)}
