"""Fetch play-by-play data for all games in a season and store raw JSON in S3.

Reads game IDs from the season games file in S3, fetches PBP from the NBA API,
and stores one JSON file per game in S3.

Usage (from repo root):
    python -m scripts.nba_stats.fetch_play_by_play                          # 2024-25 season
    python -m scripts.nba_stats.fetch_play_by_play --season 2023-24
    python -m scripts.nba_stats.fetch_play_by_play --max-games 5            # quick test

~1,400 games x 0.6s throttle = ~15 minutes for a full season.
"""

from __future__ import annotations

import argparse
import time

from app.clients.nba_stats import fetch_play_by_play
from app.services.s3_raw import get_raw, put_raw, list_keys


def get_game_ids_from_s3(season: str) -> list[str]:
    """Read game IDs from the season games file in S3."""
    raw = get_raw(f"nba/games/season_{season}.json")
    game_ids = sorted(set(row["GAME_ID"] for row in raw))
    return game_ids


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default="2024-25",
                    help="NBA season, e.g. 2024-25 (default: 2024-25)")
    ap.add_argument("--max-games", type=int, default=None,
                    help="Cap number of games (for testing)")
    ap.add_argument("--throttle", type=float, default=0.6,
                    help="Seconds between API calls (default: 0.6)")
    args = ap.parse_args()

    try:
        game_ids = get_game_ids_from_s3(args.season)
    except Exception as e:
        print(f"Could not read game IDs from S3: {e}")
        print("Run `python -m scripts.fetch_nba_games` first to populate S3.")
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
