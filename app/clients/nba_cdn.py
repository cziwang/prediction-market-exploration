"""Client for the NBA CDN live data API (cdn.nba.com).

Returns raw JSON — no transformation. These endpoints serve static JSON
files and don't require authentication or special headers.
"""

from __future__ import annotations

import requests

BASE_URL = "https://cdn.nba.com/static/json/liveData"

_session = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })
    return _session


def _get(path: str) -> dict:
    resp = _get_session().get(f"{BASE_URL}/{path}")
    resp.raise_for_status()
    return resp.json()


def fetch_scoreboard() -> dict:
    """Fetch today's scoreboard (all games, scores, status)."""
    return _get("scoreboard/todaysScoreboard_00.json")


def fetch_boxscore(game_id: str) -> dict:
    """Fetch live box score for a game."""
    return _get(f"boxscore/boxscore_{game_id}.json")


def fetch_play_by_play(game_id: str) -> dict:
    """Fetch live play-by-play for a game."""
    return _get(f"playbyplay/playbyplay_{game_id}.json")


def fetch_odds() -> dict:
    """Fetch betting odds for today's games."""
    return _get("odds/odds_todaysGames.json")
