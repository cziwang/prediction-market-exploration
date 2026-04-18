"""Client for the NBA CDN live data API (cdn.nba.com).

Returns raw JSON — no transformation. These endpoints serve static JSON
files and don't require authentication, but cdn.nba.com rejects the
default Python User-Agent with 403, so a browser-shaped UA is included.

Module-level URL constants and HEADERS are exported for reuse by the
live ingester under `scripts/live/nba_cdn/`, which builds its own
`httpx.AsyncClient` but wants the same endpoints and headers.
"""

from __future__ import annotations

import requests

BASE_URL = "https://cdn.nba.com/static/json/liveData"
SCOREBOARD_URL = f"{BASE_URL}/scoreboard/todaysScoreboard_00.json"
ODDS_URL = f"{BASE_URL}/odds/odds_todaysGames.json"
PBP_URL = f"{BASE_URL}/playbyplay/playbyplay_{{}}.json"
BOXSCORE_URL = f"{BASE_URL}/boxscore/boxscore_{{}}.json"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

_session = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
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
