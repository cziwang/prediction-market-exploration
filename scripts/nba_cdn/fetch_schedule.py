"""Fetch the full NBA season schedule from cdn.nba.com and store in S3.

The CDN schedule endpoint serves the current season's full schedule
(preseason through finals) including game IDs, dates, teams, and status.

Usage:
    python -m scripts.nba_cdn.fetch_schedule
"""

from __future__ import annotations

from app.clients.nba_cdn import fetch_schedule
from app.services.s3_raw import put_raw


def main() -> None:
    print("Fetching NBA schedule from cdn.nba.com...")
    data = fetch_schedule()

    league = data.get("leagueSchedule", {})
    season = league.get("seasonYear", "unknown")
    game_dates = league.get("gameDates", [])
    total_games = sum(len(d.get("games", [])) for d in game_dates)
    completed = sum(
        1
        for d in game_dates
        for g in d.get("games", [])
        if g.get("gameStatus") == 3
    )

    s3_key = put_raw(
        source="nba_cdn",
        dataset="schedule",
        key=f"season_{season}.json",
        data=data,
    )
    print(f"  season {season}: {total_games} games ({completed} completed)")
    print(f"  stored -> s3://prediction-markets-data/{s3_key}")


if __name__ == "__main__":
    main()
