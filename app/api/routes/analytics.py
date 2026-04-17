from fastapi import APIRouter, Query

from app.db.session import get_db, rows_to_dicts

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/summary")
def summary() -> dict:
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


@router.get("/biggest-swings")
def biggest_swings(limit: int = Query(20, ge=1, le=100)) -> dict:
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


@router.get("/volume-leaders")
def volume_leaders(limit: int = Query(20, ge=1, le=100)) -> dict:
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


@router.get("/result-distribution")
def result_distribution() -> dict:
    conn = get_db()
    rows = conn.execute(
        """SELECT result, COUNT(*) AS count, SUM(volume) AS total_volume
           FROM markets
           WHERE result IS NOT NULL AND result != ''
           GROUP BY result"""
    ).fetchall()
    conn.close()
    return {"distribution": rows_to_dicts(rows)}
