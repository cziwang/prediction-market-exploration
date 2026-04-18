"""Fetch historical Kalshi trades for NBA markets and store in S3.

Reads market tickers from S3 (run fetch_kalshi_historical_markets first), then
fetches all trades for each market.

Usage:
    python -m scripts.fetch_kalshi_trades
    python -m scripts.fetch_kalshi_trades --max-markets 10
"""

from __future__ import annotations

import argparse
import time

from app.clients.kalshi import paginate_historical_trades
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
        print("Run `python -m scripts.fetch_kalshi_historical_markets` first.")
        return

    tickers = [m["ticker"] for m in markets]
    if args.max_markets:
        tickers = tickers[:args.max_markets]

    # Skip tickers already fetched
    existing = set(list_keys("kalshi", "historical_trades"))
    to_fetch = [t for t in tickers if f"kalshi/historical_trades/{t}.json" not in existing]
    print(f"{len(tickers)} markets, {len(tickers) - len(to_fetch)} already in S3, {len(to_fetch)} to fetch")

    for i, ticker in enumerate(to_fetch, 1):
        try:
            trades = list(paginate_historical_trades(ticker=ticker))
        except Exception as e:
            print(f"  [{i}/{len(to_fetch)}] {ticker}: error - {e}")
            time.sleep(args.throttle)
            continue

        put_raw(
            source="kalshi",
            dataset="historical_trades",
            key=f"{ticker}.json",
            data=trades,
        )
        print(f"  [{i}/{len(to_fetch)}] {ticker}: {len(trades)} trades")
        time.sleep(args.throttle)

    print(f"\nDone. Stored trades for {len(to_fetch)} markets.")


if __name__ == "__main__":
    main()
