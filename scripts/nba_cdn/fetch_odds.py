"""Fetch today's odds from cdn.nba.com and store in S3.

Usage:
    python -m scripts.fetch_cdn_odds
"""

from __future__ import annotations

from datetime import date

from app.clients.nba_cdn import fetch_odds
from app.services.s3_raw import put_raw


def main() -> None:
    print("Fetching today's odds from cdn.nba.com...")
    data = fetch_odds()

    games = data.get("games", [])
    today = date.today().isoformat()

    s3_key = put_raw(
        source="nba_cdn",
        dataset="odds",
        key=f"{today}.json",
        data=data,
    )
    print(f"  {len(games)} games with odds -> s3://prediction-markets-data/{s3_key}")


if __name__ == "__main__":
    main()
