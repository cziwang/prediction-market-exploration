"""Fetch settled NBA game markets from Kalshi and store them in SQLite.

Usage (from repo root):
    python -m scripts.fetch_nba_history                 # markets only
    python -m scripts.fetch_nba_history --candles       # + 1-minute OHLC per market
    python -m scripts.fetch_nba_history --candles --period 60
    python -m scripts.fetch_nba_history --candles --refresh-candles

Output: ./data/kalshi_nba.db
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from datetime import datetime, timezone

from kalshi_python.api.markets_api import MarketsApi
from kalshi_python.exceptions import ApiException

from app.clients.kalshi import make_client, paginate_markets
from app.core.config import DATA_DIR, DB_PATH, SERIES
from app.db.session import init_db


def iso_to_ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def upsert_markets(
    conn: sqlite3.Connection, api: MarketsApi, max_markets: int | None = None
) -> int:
    prefix = f"{SERIES}-"
    seen: set[str] = set()
    total = 0

    print(f"  -> get_markets(series_ticker={SERIES}, status=settled)")
    page_count = 0
    for m in paginate_markets(api, series_ticker=SERIES, status="settled", limit=1000):
        if max_markets is not None and len(seen) >= max_markets:
            break
        t = m.ticker
        if not t or t in seen or not t.startswith(prefix):
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
                m.event_ticker,
                SERIES,
                m.title,
                m.subtitle,
                None,
                m.open_time,
                m.close_time,
                m.status,
                m.result,
                m.volume,
                json.dumps(m.to_dict()),
            ),
        )
        total += 1
        page_count += 1
        if page_count % 500 == 0:
            conn.commit()
            print(f"    upserted {page_count} so far...")
    conn.commit()
    return total


def fetch_candlesticks(
    conn: sqlite3.Connection,
    api: MarketsApi,
    period_interval: int,
    skip_existing: bool,
    throttle_s: float,
    max_markets: int | None = None,
) -> None:
    limit_clause = f"LIMIT {int(max_markets)}" if max_markets is not None else ""
    rows = conn.execute(
        f"""
        SELECT ticker, open_time, close_time
        FROM markets
        WHERE result IS NOT NULL AND result != ''
          AND open_time IS NOT NULL
          AND close_time IS NOT NULL
        ORDER BY close_time DESC
        {limit_clause}
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
            resp = api.get_market_candlesticks(
                ticker=SERIES,
                market_ticker=ticker,
                start_ts=start_ts,
                end_ts=end_ts,
                period_interval=period_interval,
            )
        except ApiException as e:
            print(f"  [{i}/{n}] {ticker}: API error {e.status}")
            continue

        candles = resp.candlesticks or []
        for c in candles:
            conn.execute(
                """
                INSERT OR REPLACE INTO candlesticks
                (ticker, end_period_ts, price_open, price_high, price_low,
                 price_close, volume, open_interest, yes_bid_close, yes_ask_close)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    c.end_ts,
                    c.open,
                    c.high,
                    c.low,
                    c.close,
                    c.volume,
                    None,
                    None,
                    None,
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


def export_csv(conn: sqlite3.Connection) -> None:
    exclude = {"markets": {"raw_json"}, "candlesticks": set()}
    for table, skip in exclude.items():
        cur = conn.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        keep = [i for i, c in enumerate(cols) if c not in skip]
        out_cols = [cols[i] for i in keep]
        path = DATA_DIR / f"{table}.csv"
        rows_written = 0
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(out_cols)
            for row in cur:
                w.writerow([row[i] for i in keep])
                rows_written += 1
        print(f"  wrote {rows_written:>6} rows -> {path}")


def summarize(conn: sqlite3.Connection) -> None:
    m_total = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    m_settled = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE result IS NOT NULL AND result != ''"
    ).fetchone()[0]
    c_total = conn.execute("SELECT COUNT(*) FROM candlesticks").fetchone()[0]
    c_tickers = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM candlesticks"
    ).fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(close_time), MAX(close_time) FROM markets "
        "WHERE result IS NOT NULL AND result != ''"
    ).fetchone()
    print()
    print(f"Markets:      {m_total} total, {m_settled} settled")
    print(f"Candles:      {c_total} rows across {c_tickers} markets")
    if date_range[0]:
        print(f"Date range:   {date_range[0]} -> {date_range[1]}")
    print(f"DB:           {DB_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candles", action="store_true",
                    help="Also fetch OHLC candlesticks for each settled market")
    ap.add_argument("--period", type=int, default=1, choices=[1, 60, 1440],
                    help="Candlestick interval in minutes (default: 1)")
    ap.add_argument("--refresh-candles", action="store_true",
                    help="Re-fetch candlesticks even if already stored for this period")
    ap.add_argument("--throttle", type=float, default=0.1,
                    help="Seconds to sleep between candlestick requests (default: 0.1)")
    ap.add_argument("--skip-markets", action="store_true",
                    help="Skip refreshing market metadata (only fetch candles)")
    ap.add_argument("--csv", action="store_true",
                    help="After fetching, export markets and candlesticks tables to CSV")
    ap.add_argument("--csv-only", action="store_true",
                    help="Skip any API calls; just export existing DB tables to CSV")
    ap.add_argument("--max-markets", type=int, default=None,
                    help="Cap markets fetched (for quick tests). Default: unlimited")
    args = ap.parse_args()

    conn = init_db()

    if not args.csv_only:
        api = make_client()
        print("Kalshi SDK client ready")

        if not args.skip_markets:
            print(f"\nFetching markets for series {SERIES}...")
            n = upsert_markets(conn, api, max_markets=args.max_markets)
            print(f"  upserted {n} unique markets")

        if args.candles:
            print(f"\nFetching {args.period}-minute candlesticks...")
            fetch_candlesticks(
                conn,
                api,
                period_interval=args.period,
                skip_existing=not args.refresh_candles,
                throttle_s=args.throttle,
                max_markets=args.max_markets,
            )

    if args.csv or args.csv_only:
        print("\nExporting CSV...")
        export_csv(conn)

    summarize(conn)


if __name__ == "__main__":
    main()
