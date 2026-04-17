from fastapi import APIRouter, HTTPException, Query

from app.db.session import get_db, rows_to_dicts

router = APIRouter(tags=["Events"])


@router.get("/events")
def list_events(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
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


@router.get("/events/{event_ticker}")
def get_event(event_ticker: str) -> dict:
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
