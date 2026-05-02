"""Fetch all historical Kalshi NBA markets and store in S3.

Usage:
    python -m scripts.kalshi.fetch_historical_markets                    # all NBA series
    python -m scripts.kalshi.fetch_historical_markets --series KXNBAGAME # single series
"""

from __future__ import annotations

import argparse
import time

from app.clients.kalshi_rest import paginate_historical_markets
from app.services.s3_raw import put_raw

ALL_NBA_SERIES = [
    # Game-level
    "KXNBAGAME",      # win/loss
    "KXNBASPREAD",    # point spread
    "KXNBATOTAL",     # total points over/under
    # Player props
    "KXNBAPTS",       # player points
    "KXNBAREB",       # player rebounds
    "KXNBAAST",       # player assists
    "KXNBA3PT",       # player threes
    "KXNBABLK",       # player blocks
    "KXNBASTL",       # player steals
    # Season / Playoff
    "KXNBA",          # NBA Finals winner
    "KXNBASERIES",    # playoff series winner
    "KXNBAPLAYOFF",   # playoff qualifier
    "KXNBAALLSTAR",   # all-star game
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", default=None,
                    help="Single series ticker (default: fetch all NBA series)")
    args = ap.parse_args()

    series_list = [args.series] if args.series else ALL_NBA_SERIES

    for series in series_list:
        print(f"Fetching historical markets for {series}...")
        markets = list(paginate_historical_markets(series_ticker=series))
        if not markets:
            print(f"  (empty)")
            continue

        s3_key = put_raw(
            source="kalshi",
            dataset="historical_markets",
            key=f"{series}.json",
            data=markets,
        )
        print(f"  {len(markets)} markets -> s3://prediction-markets-data/{s3_key}")
        time.sleep(0.5)

    print("\nDone.")


if __name__ == "__main__":
    main()
