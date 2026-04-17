from typing import Any

from fastapi import APIRouter, Query

from app.db.session import get_db, rows_to_dicts

router = APIRouter(tags=["Candlesticks"])


@router.get("/markets/{ticker}/candlesticks")
def get_candlesticks(
    ticker: str,
    start_ts: int | None = Query(None, description="Unix timestamp lower bound"),
    end_ts: int | None = Query(None, description="Unix timestamp upper bound"),
    limit: int = Query(5000, ge=1, le=50000),
) -> dict:
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
