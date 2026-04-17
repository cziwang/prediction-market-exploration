"""Fetch NBA game results and store raw JSON in S3.

Usage (from repo root):
    python -m scripts.fetch_nba_games                          # current season
    python -m scripts.fetch_nba_games --season 2023-24         # specific season
"""

from __future__ import annotations

import argparse

from app.clients.nba import fetch_season_games
from app.services.s3_raw import put_raw


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default="2024-25",
                    help="NBA season, e.g. 2024-25 (default: 2024-25)")
    args = ap.parse_args()

    print(f"Fetching NBA games for {args.season}...")

    raw_rows = fetch_season_games(args.season)
    print(f"  fetched {len(raw_rows)} team-game rows")

    s3_key = put_raw(
        source="nba",
        dataset="games",
        key=f"season_{args.season}.json",
        data=raw_rows,
    )
    print(f"  stored -> s3://prediction-markets-data/{s3_key}")


if __name__ == "__main__":
    main()
