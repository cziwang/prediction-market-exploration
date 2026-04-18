"""Fetch today's scoreboard from cdn.nba.com and store in S3.

Usage:
    python -m scripts.fetch_cdn_scoreboard
"""

from __future__ import annotations

from datetime import date

from app.clients.nba_cdn import fetch_scoreboard
from app.services.s3_raw import put_raw


def main() -> None:
    print("Fetching today's scoreboard from cdn.nba.com...")
    data = fetch_scoreboard()

    games = data.get("scoreboard", {}).get("games", [])
    today = date.today().isoformat()

    s3_key = put_raw(
        source="nba_cdn",
        dataset="scoreboard",
        key=f"{today}.json",
        data=data,
    )
    print(f"  {len(games)} games -> s3://prediction-markets-data/{s3_key}")


if __name__ == "__main__":
    main()
