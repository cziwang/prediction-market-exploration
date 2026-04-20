"""Fetch box scores from cdn.nba.com and store in S3.

Two modes:
  Default  — fetch box scores for today's games (from scoreboard).
  --season — backfill box scores for all completed games in a season schedule
             stored in S3. Skips games already fetched.

Usage:
    python -m scripts.nba_cdn.fetch_boxscores                   # today's games
    python -m scripts.nba_cdn.fetch_boxscores --season 2025-26  # backfill season
    python -m scripts.nba_cdn.fetch_boxscores --season 2025-26 --max-games 10
"""

from __future__ import annotations

import argparse
import time

from app.clients.nba_cdn import fetch_scoreboard, fetch_boxscore
from app.services.s3_raw import get_raw, put_raw, list_keys


def _game_ids_from_scoreboard() -> list[str]:
    """Get game IDs from today's scoreboard."""
    scoreboard = fetch_scoreboard()
    games = scoreboard.get("scoreboard", {}).get("games", [])
    return [g["gameId"] for g in games]


def _game_ids_from_schedule(season: str) -> list[str]:
    """Get completed game IDs from a season schedule in S3."""
    data = get_raw(f"nba_cdn/schedule/season_{season}.json")
    game_dates = data.get("leagueSchedule", {}).get("gameDates", [])
    return [
        g["gameId"]
        for d in game_dates
        for g in d.get("games", [])
        if g.get("gameStatus") == 3
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default=None,
                    help="Backfill a full season, e.g. 2025-26 (requires schedule in S3)")
    ap.add_argument("--max-games", type=int, default=None,
                    help="Cap number of games to fetch (for testing)")
    ap.add_argument("--throttle", type=float, default=0.1,
                    help="Seconds between requests (default: 0.1)")
    args = ap.parse_args()

    if args.season:
        print(f"Loading schedule for {args.season} from S3...")
        try:
            game_ids = _game_ids_from_schedule(args.season)
        except Exception as e:
            print(f"Could not read schedule from S3: {e}")
            print("Run `python -m scripts.nba_cdn.fetch_schedule` first.")
            return
    else:
        print("Fetching scoreboard to get today's game IDs...")
        game_ids = _game_ids_from_scoreboard()

    if args.max_games:
        game_ids = game_ids[: args.max_games]

    existing_keys = set(list_keys("nba_cdn", "boxscore"))
    to_fetch = [gid for gid in game_ids if f"nba_cdn/boxscore/{gid}.json" not in existing_keys]

    print(f"{len(game_ids)} games, {len(game_ids) - len(to_fetch)} already in S3, {len(to_fetch)} to fetch")

    errors = 0
    for i, game_id in enumerate(to_fetch, 1):
        try:
            data = fetch_boxscore(game_id)
        except Exception as e:
            print(f"  [{i}/{len(to_fetch)}] {game_id}: error - {e}")
            errors += 1
            time.sleep(args.throttle)
            continue

        put_raw(
            source="nba_cdn",
            dataset="boxscore",
            key=f"{game_id}.json",
            data=data,
        )
        home = data.get("game", {}).get("homeTeam", {}).get("teamTricode", "?")
        away = data.get("game", {}).get("awayTeam", {}).get("teamTricode", "?")
        print(f"  [{i}/{len(to_fetch)}] {game_id} ({away} @ {home})")
        time.sleep(args.throttle)

    print(f"\nDone. Fetched {len(to_fetch) - errors} games, {errors} errors.")


if __name__ == "__main__":
    main()
