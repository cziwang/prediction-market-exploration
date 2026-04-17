"""Fetch play-by-play data for all games in a season and store raw JSON in S3.

Reads game IDs from the local games table, fetches PBP from the NBA API,
and stores one JSON file per game in S3.

Usage (from repo root):
    python -m scripts.fetch_play_by_play                          # 2024-25 season
    python -m scripts.fetch_play_by_play --season 2023-24
    python -m scripts.fetch_play_by_play --max-games 5            # quick test

~1,400 games x 0.6s throttle = ~15 minutes for a full season.
"""

from __future__ import annotations

import argparse
import time

from app.clients.nba import fetch_play_by_play
from app.db.session import get_db
from app.services.s3_raw import put_raw, list_keys


def get_game_ids(season_id: str) -> list[str]:
    """Get unique game IDs from the games table for a season."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT game_id FROM games WHERE season_id = ? ORDER BY game_date",
        (season_id,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def season_to_season_id(season: str) -> str:
    """Convert '2024-25' to '22024' (NBA season_id format)."""
    year = season.split("-")[0]
    return f"2{year}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default="2024-25",
                    help="NBA season, e.g. 2024-25 (default: 2024-25)")
    ap.add_argument("--max-games", type=int, default=None,
                    help="Cap number of games (for testing)")
    ap.add_argument("--throttle", type=float, default=0.6,
                    help="Seconds between API calls (default: 0.6)")
    args = ap.parse_args()

    season_id = season_to_season_id(args.season)
    game_ids = get_game_ids(season_id)

    if not game_ids:
        print(f"No games found for season {args.season} (season_id={season_id}).")
        print("Run `python -m scripts.fetch_nba_games` first.")
        return

    if args.max_games:
        game_ids = game_ids[: args.max_games]

    # Check which games already have PBP in S3
    existing_keys = set(list_keys("nba", "play_by_play"))
    to_fetch = [gid for gid in game_ids if f"nba/play_by_play/{gid}.json" not in existing_keys]

    print(f"{len(game_ids)} games in season, {len(game_ids) - len(to_fetch)} already in S3, {len(to_fetch)} to fetch")

    for i, game_id in enumerate(to_fetch, 1):
        try:
            raw_plays = fetch_play_by_play(game_id)
        except Exception as e:
            print(f"  [{i}/{len(to_fetch)}] {game_id}: error - {e}")
            time.sleep(args.throttle)
            continue

        put_raw(
            source="nba",
            dataset="play_by_play",
            key=f"{game_id}.json",
            data=raw_plays,
        )

        print(f"  [{i}/{len(to_fetch)}] {game_id}: {len(raw_plays)} plays")
        time.sleep(args.throttle)

    print(f"\nDone. Stored PBP for {len(to_fetch)} games in S3.")


if __name__ == "__main__":
    main()
