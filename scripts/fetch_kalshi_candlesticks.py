"""Fetch historical Kalshi candlesticks for NBA markets and store in S3.

Reads market tickers from S3 (run fetch_kalshi_markets first), then
fetches hourly candlestick data for each market's active period.

Usage:
    python -m scripts.fetch_kalshi_candlesticks
    python -m scripts.fetch_kalshi_candlesticks --max-markets 10
    python -m scripts.fetch_kalshi_candlesticks --interval 1440   # daily candles
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime

from app.clients.kalshi import get_historical_candlesticks
from app.core.config import SERIES
from app.services.s3_raw import get_raw, put_raw, list_keys


def _iso_to_ts(iso: str) -> int:
    """Convert ISO 8601 string to Unix timestamp."""
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default=SERIES,
                    help=f"Series ticker (default: {SERIES})")
    ap.add_argument("--max-markets", type=int, default=None,
                    help="Cap number of markets (for testing)")
    ap.add_argument("--interval", type=int, default=60,
                    help="Candle interval in minutes: 1, 60, or 1440 (default: 60)")
    ap.add_argument("--throttle", type=float, default=0.3,
                    help="Seconds between API calls (default: 0.3)")
    args = ap.parse_args()

    try:
        markets = get_raw(f"kalshi/historical_markets/{args.series}.json")
    except Exception as e:
        print(f"Could not read markets from S3: {e}")
        print("Run `python -m scripts.fetch_kalshi_markets` first.")
        return

    if args.max_markets:
        markets = markets[:args.max_markets]

    existing = set(list_keys("kalshi", "historical_candlesticks"))
    to_fetch = [m for m in markets if f"kalshi/historical_candlesticks/{m['ticker']}.json" not in existing]
    print(f"{len(markets)} markets, {len(markets) - len(to_fetch)} already in S3, {len(to_fetch)} to fetch")

    for i, market in enumerate(to_fetch, 1):
        ticker = market["ticker"]
        try:
            start_ts = _iso_to_ts(market["open_time"])
            end_ts = _iso_to_ts(market["close_time"])
            data = get_historical_candlesticks(
                ticker, start_ts=start_ts, end_ts=end_ts,
                period_interval=args.interval,
            )
        except Exception as e:
            print(f"  [{i}/{len(to_fetch)}] {ticker}: error - {e}")
            time.sleep(args.throttle)
            continue

        candles = data.get("candlesticks") or []
        put_raw(
            source="kalshi",
            dataset="historical_candlesticks",
            key=f"{ticker}.json",
            data=data,
        )
        print(f"  [{i}/{len(to_fetch)}] {ticker}: {len(candles)} candles")
        time.sleep(args.throttle)

    print(f"\nDone. Stored candlesticks for {len(to_fetch)} markets.")


if __name__ == "__main__":
    main()
