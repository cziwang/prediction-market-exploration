"""Fetch NBA game results and store in S3 (raw) + SQLite (normalized).

Usage (from repo root):
    python -m scripts.fetch_nba_games                          # current season
    python -m scripts.fetch_nba_games --season 2023-24         # specific season
    python -m scripts.fetch_nba_games --season 2023-24 --skip-s3  # skip S3, local only
"""

from __future__ import annotations

import argparse

from app.repositories.games import init_games_table


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default="2024-25",
                    help="NBA season, e.g. 2024-25 (default: 2024-25)")
    ap.add_argument("--skip-s3", action="store_true",
                    help="Skip writing raw data to S3")
    args = ap.parse_args()

    # Ensure table exists
    init_games_table()

    print(f"Fetching NBA games for {args.season}...")

    # Fetch from NBA API
    from app.clients.nba import fetch_season_games
    raw_rows = fetch_season_games(args.season)
    print(f"  fetched {len(raw_rows)} team-game rows")

    # Store raw in S3
    if not args.skip_s3:
        from app.services.s3_raw import put_raw
        s3_key = put_raw(
            source="nba",
            dataset="games",
            key=f"season_{args.season}.json",
            data=raw_rows,
        )
        print(f"  raw data -> s3://{args.season}/{s3_key}")

    # Normalize and write to DB
    from app.services.nba_ingest import _normalize_game_row
    normalized = [_normalize_game_row(row) for row in raw_rows]

    from app.repositories.games import upsert_games
    count = upsert_games(normalized)
    print(f"  upserted {count} rows to games table")


if __name__ == "__main__":
    main()
