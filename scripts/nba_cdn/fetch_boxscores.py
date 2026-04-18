"""Fetch box scores for today's games from cdn.nba.com and store in S3.

Pulls the scoreboard first to get game IDs, then fetches each box score.

Usage:
    python -m scripts.fetch_cdn_boxscores
"""

from __future__ import annotations

from app.clients.nba_cdn import fetch_scoreboard, fetch_boxscore
from app.services.s3_raw import put_raw


def main() -> None:
    print("Fetching scoreboard to get today's game IDs...")
    scoreboard = fetch_scoreboard()
    games = scoreboard.get("scoreboard", {}).get("games", [])
    game_ids = [g["gameId"] for g in games]
    print(f"  {len(game_ids)} games today")

    for game_id in game_ids:
        try:
            data = fetch_boxscore(game_id)
        except Exception as e:
            print(f"  {game_id}: error - {e}")
            continue

        s3_key = put_raw(
            source="nba_cdn",
            dataset="boxscore",
            key=f"{game_id}.json",
            data=data,
        )
        home = data.get("game", {}).get("homeTeam", {}).get("teamTricode", "?")
        away = data.get("game", {}).get("awayTeam", {}).get("teamTricode", "?")
        print(f"  {game_id} ({away} @ {home}) -> s3://prediction-markets-data/{s3_key}")

    print(f"\nDone. Stored {len(game_ids)} box scores.")


if __name__ == "__main__":
    main()
