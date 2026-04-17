"""Ingest service for NBA game data.

Fetches raw data from the NBA API, stores raw JSON in S3,
normalizes it, and writes to Postgres.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.clients import nba as nba_client
from app.services import s3_raw


def ingest_season(season: str) -> int:
    """Fetch a full season of game results, store raw + normalized.

    Returns the number of games ingested.
    """
    raw_rows = nba_client.fetch_season_games(season)

    # Store raw response in S3
    s3_raw.put_raw(
        source="nba",
        dataset="games",
        key=f"season_{season}.json",
        data=raw_rows,
    )

    # Normalize: two rows per game (one per team) → one row per team-game
    normalized = [_normalize_game_row(row) for row in raw_rows]

    # Write to Postgres
    from app.repositories import games as games_repo
    count = games_repo.upsert_games(normalized)

    return count


def _normalize_game_row(raw: dict) -> dict:
    """Map raw NBA API fields to our canonical schema."""
    return {
        "game_id": raw["GAME_ID"],
        "season_id": raw["SEASON_ID"],
        "game_date": raw["GAME_DATE"],
        "team_id": raw["TEAM_ID"],
        "team_abbr": raw["TEAM_ABBREVIATION"],
        "team_name": raw["TEAM_NAME"],
        "matchup": raw["MATCHUP"],
        "wl": raw["WL"],
        "pts": raw["PTS"],
        "fgm": raw["FGM"],
        "fga": raw["FGA"],
        "fg_pct": raw["FG_PCT"],
        "fg3m": raw["FG3M"],
        "fg3a": raw["FG3A"],
        "fg3_pct": raw["FG3_PCT"],
        "ftm": raw["FTM"],
        "fta": raw["FTA"],
        "ft_pct": raw["FT_PCT"],
        "oreb": raw["OREB"],
        "dreb": raw["DREB"],
        "reb": raw["REB"],
        "ast": raw["AST"],
        "stl": raw["STL"],
        "blk": raw["BLK"],
        "tov": raw["TOV"],
        "pf": raw["PF"],
        "plus_minus": raw["PLUS_MINUS"],
    }
