"""Fetch historical Kalshi trades for NBA markets and store in S3.

Reads market tickers from S3 (run fetch_kalshi_historical_markets first), then
fetches all trades for each market using a thread pool for concurrency.

Usage:
    python -m scripts.kalshi.fetch_historical_trades                    # all NBA series
    python -m scripts.kalshi.fetch_historical_trades --series KXNBAGAME # single series
    python -m scripts.kalshi.fetch_historical_trades --workers 4        # 4 concurrent fetches
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.clients.kalshi_rest import paginate_historical_trades
from app.services.s3_raw import get_raw, put_raw, list_keys
from scripts.kalshi.fetch_historical_markets import ALL_NBA_SERIES


def _fetch_one(ticker: str) -> tuple[str, int]:
    """Fetch trades for a single market and store in S3. Returns (ticker, count)."""
    trades = list(paginate_historical_trades(ticker=ticker))
    put_raw(
        source="kalshi",
        dataset="historical_trades",
        key=f"{ticker}.json",
        data=trades,
    )
    return ticker, len(trades)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default=None,
                    help="Single series ticker (default: fetch all NBA series)")
    ap.add_argument("--max-markets", type=int, default=None,
                    help="Cap number of markets per series (for testing)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Number of concurrent fetches (default: 4)")
    args = ap.parse_args()

    series_list = [args.series] if args.series else ALL_NBA_SERIES

    # Collect all tickers across series
    all_tickers: list[str] = []
    for series in series_list:
        try:
            markets = get_raw(f"kalshi/historical_markets/{series}.json")
        except Exception as e:
            print(f"Could not read {series} from S3: {e}")
            continue
        tickers = [m["ticker"] for m in markets]
        if args.max_markets:
            tickers = tickers[:args.max_markets]
        all_tickers.extend(tickers)

    existing = set(list_keys("kalshi", "historical_trades"))
    to_fetch = [t for t in all_tickers if f"kalshi/historical_trades/{t}.json" not in existing]
    print(f"{len(all_tickers)} total markets, {len(all_tickers) - len(to_fetch)} already in S3, {len(to_fetch)} to fetch")
    print(f"Using {args.workers} workers\n")

    done = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_fetch_one, t): t for t in to_fetch}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                _, count = future.result()
                done += 1
                print(f"  [{done + errors}/{len(to_fetch)}] {ticker}: {count} trades")
            except Exception as e:
                errors += 1
                print(f"  [{done + errors}/{len(to_fetch)}] {ticker}: error - {e}")

    print(f"\nDone. {done} succeeded, {errors} errors.")


if __name__ == "__main__":
    main()
