from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.db.session import get_db, rows_to_dicts

router = APIRouter(tags=["Markets"])


@router.get("/markets")
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


@router.get("/markets/{ticker}")
def get_market(ticker: str) -> dict:
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
