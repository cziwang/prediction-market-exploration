"""Fetch historical Kalshi orders for NBA markets and store in S3.

Requires auth. Reads market tickers from S3 (run fetch_kalshi_markets first).

Usage:
    python -m scripts.fetch_kalshi_orders
    python -m scripts.fetch_kalshi_orders --max-markets 10
"""

from __future__ import annotations

import argparse
import time

from app.clients.kalshi import paginate_historical_orders
from app.core.config import SERIES
from app.services.s3_raw import get_raw, put_raw, list_keys


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default=SERIES,
                    help=f"Series ticker (default: {SERIES})")
    ap.add_argument("--max-markets", type=int, default=None,
                    help="Cap number of markets (for testing)")
    ap.add_argument("--throttle", type=float, default=0.3,
                    help="Seconds between API calls (default: 0.3)")
    args = ap.parse_args()

    try:
        markets = get_raw(f"kalshi/historical_markets/{args.series}.json")
    except Exception as e:
        print(f"Could not read markets from S3: {e}")
        print("Run `python -m scripts.fetch_kalshi_markets` first.")
        return

    tickers = [m["ticker"] for m in markets]
    if args.max_markets:
        tickers = tickers[:args.max_markets]

    existing = set(list_keys("kalshi", "historical_orders"))
    to_fetch = [t for t in tickers if f"kalshi/historical_orders/{t}.json" not in existing]
    print(f"{len(tickers)} markets, {len(tickers) - len(to_fetch)} already in S3, {len(to_fetch)} to fetch")

    for i, ticker in enumerate(to_fetch, 1):
        try:
            orders = list(paginate_historical_orders(ticker=ticker))
        except Exception as e:
            print(f"  [{i}/{len(to_fetch)}] {ticker}: error - {e}")
            time.sleep(args.throttle)
            continue

        put_raw(
            source="kalshi",
            dataset="historical_orders",
            key=f"{ticker}.json",
            data=orders,
        )
        print(f"  [{i}/{len(to_fetch)}] {ticker}: {len(orders)} orders")
        time.sleep(args.throttle)

    print(f"\nDone. Stored orders for {len(to_fetch)} markets.")


if __name__ == "__main__":
    main()
