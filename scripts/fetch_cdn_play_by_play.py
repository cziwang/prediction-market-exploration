"""Fetch play-by-play for today's games from cdn.nba.com and store in S3.

Pulls the scoreboard first to get game IDs, then fetches each PBP.

Usage:
    python -m scripts.fetch_cdn_play_by_play
"""

from __future__ import annotations

from app.clients.nba_cdn import fetch_scoreboard, fetch_play_by_play
from app.services.s3_raw import put_raw


def main() -> None:
    print("Fetching scoreboard to get today's game IDs...")
    scoreboard = fetch_scoreboard()
    games = scoreboard.get("scoreboard", {}).get("games", [])
    game_ids = [g["gameId"] for g in games]
    print(f"  {len(game_ids)} games today")

    for game_id in game_ids:
        try:
            data = fetch_play_by_play(game_id)
        except Exception as e:
            print(f"  {game_id}: error - {e}")
            continue

        actions = data.get("game", {}).get("actions", [])
        s3_key = put_raw(
            source="nba_cdn",
            dataset="play_by_play",
            key=f"{game_id}.json",
            data=data,
        )
        print(f"  {game_id}: {len(actions)} actions -> s3://prediction-markets-data/{s3_key}")

    print(f"\nDone. Stored PBP for {len(game_ids)} games.")


if __name__ == "__main__":
    main()
