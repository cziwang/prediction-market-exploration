"""Fetch all historical Kalshi NBA markets and store in S3.

Usage:
    python -m scripts.fetch_kalshi_markets
    python -m scripts.fetch_kalshi_markets --series KXNBAGAME
"""

from __future__ import annotations

import argparse

from app.clients.kalshi import paginate_historical_markets
from app.core.config import SERIES
from app.services.s3_raw import put_raw


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default=SERIES,
                    help=f"Series ticker (default: {SERIES})")
    args = ap.parse_args()

    print(f"Fetching historical markets for {args.series}...")
    markets = list(paginate_historical_markets(series_ticker=args.series))
    print(f"  {len(markets)} markets")

    s3_key = put_raw(
        source="kalshi",
        dataset="historical_markets",
        key=f"{args.series}.json",
        data=markets,
    )
    print(f"  -> s3://prediction-markets-data/{s3_key}")


if __name__ == "__main__":
    main()
