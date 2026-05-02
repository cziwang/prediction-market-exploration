"""Fetch historical Kalshi candlesticks for NBA markets and store in S3.

Reads market tickers from S3 (run fetch_kalshi_historical_markets first), then
fetches hourly candlestick data for each market using a thread pool.

Usage:
    python -m scripts.kalshi.fetch_historical_candlesticks                    # all NBA series
    python -m scripts.kalshi.fetch_historical_candlesticks --series KXNBAGAME # single series
    python -m scripts.kalshi.fetch_historical_candlesticks --workers 4        # 4 concurrent
    python -m scripts.kalshi.fetch_historical_candlesticks --interval 1440    # daily candles
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from app.clients.kalshi_rest import get_historical_candlesticks
from app.services.s3_raw import get_raw, put_raw, list_keys
from scripts.kalshi.fetch_historical_markets import ALL_NBA_SERIES


def _iso_to_ts(iso: str) -> int:
    """Convert ISO 8601 string to Unix timestamp."""
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _fetch_one(market: dict, interval: int) -> tuple[str, int]:
    """Fetch candlesticks for a single market and store in S3."""
    ticker = market["ticker"]
    start_ts = _iso_to_ts(market["open_time"])
    end_ts = _iso_to_ts(market["close_time"])
    data = get_historical_candlesticks(
        ticker, start_ts=start_ts, end_ts=end_ts, period_interval=interval,
    )
    candles = data.get("candlesticks") or []
    put_raw(
        source="kalshi",
        dataset=f"historical_candlesticks/{interval}m",
        key=f"{ticker}.json",
        data=data,
    )
    return ticker, len(candles)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default=None,
                    help="Single series ticker (default: fetch all NBA series)")
    ap.add_argument("--max-markets", type=int, default=None,
                    help="Cap number of markets per series (for testing)")
    ap.add_argument("--interval", type=int, default=60,
                    help="Candle interval in minutes: 1, 60, or 1440 (default: 60)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Number of concurrent fetches (default: 4)")
    args = ap.parse_args()

    series_list = [args.series] if args.series else ALL_NBA_SERIES

    # Collect all markets across series
    all_markets: list[dict] = []
    for series in series_list:
        try:
            markets = get_raw(f"kalshi/historical_markets/{series}.json")
        except Exception as e:
            print(f"Could not read {series} from S3: {e}")
            continue
        if args.max_markets:
            markets = markets[:args.max_markets]
        all_markets.extend(markets)

    dataset = f"historical_candlesticks/{args.interval}m"
    existing = set(list_keys("kalshi", dataset))
    to_fetch = [m for m in all_markets if f"kalshi/{dataset}/{m['ticker']}.json" not in existing]
    print(f"{len(all_markets)} total markets, {len(all_markets) - len(to_fetch)} already in S3, {len(to_fetch)} to fetch")
    print(f"Using {args.workers} workers\n")

    done = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_fetch_one, m, args.interval): m["ticker"] for m in to_fetch}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                _, count = future.result()
                done += 1
                print(f"  [{done + errors}/{len(to_fetch)}] {ticker}: {count} candles")
            except Exception as e:
                errors += 1
                print(f"  [{done + errors}/{len(to_fetch)}] {ticker}: error - {e}")

    print(f"\nDone. {done} succeeded, {errors} errors.")


if __name__ == "__main__":
    main()
