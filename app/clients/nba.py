"""Client for the NBA stats API (via nba_api package).

Returns raw DataFrames — no transformation. Normalization happens in
the ingest service layer.
"""

from __future__ import annotations

from nba_api.stats.endpoints import leaguegamefinder, playbyplayv3


def fetch_season_games(season: str) -> list[dict]:
    """Fetch all game results for a season.

    Args:
        season: NBA season string, e.g. "2024-25"

    Returns:
        List of raw row dicts from the LeagueGameFinder endpoint.
        Two rows per game (one per team).
    """
    gf = leaguegamefinder.LeagueGameFinder(
        season_nullable=season,
        league_id_nullable="00",
    )
    df = gf.get_data_frames()[0]
    return df.to_dict(orient="records")


def fetch_play_by_play(game_id: str) -> list[dict]:
    """Fetch play-by-play data for a single game.

    Args:
        game_id: 10-digit NBA game ID, e.g. "0022400001"

    Returns:
        List of raw row dicts from the PlayByPlayV3 endpoint.
    """
    pbp = playbyplayv3.PlayByPlayV3(
        game_id=game_id,
        start_period=1,
        end_period=10,  # covers OT periods
    )
    df = pbp.get_data_frames()[0]
    return df.to_dict(orient="records")
