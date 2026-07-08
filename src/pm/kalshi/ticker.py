"""Kalshi NBA ticker parsing.

Game-winner tickers encode the matchup and the YES side:

    KXNBAGAME-26APR18ATLNYK-NYK
    └series──┘ └event_code─┘ └yes side┘
               26APR18: date (Apr 18, 2026)
               ATLNYK:  away tricode + home tricode (away first, home second)
               -NYK:    YES pays if NYK wins

Other NBA series (KXNBATOTAL, KXNBASPREAD, ...) share the event-code segment
but their YES side is not a team — only series and event_code parse generally.
"""

import re
from dataclasses import dataclass

_GAME = re.compile(
    r"^(?P<series>KXNBAGAME)-"
    r"(?P<date>\d{2}[A-Z]{3}\d{2})"
    r"(?P<away>[A-Z]{3})(?P<home>[A-Z]{3})-"
    r"(?P<yes_team>[A-Z]{3})$"
)
_ANY = re.compile(
    r"^(?P<series>KXNBA[A-Z0-9]*)-"
    r"(?P<date>\d{2}[A-Z]{3}\d{2})"
    r"(?P<away>[A-Z]{3})(?P<home>[A-Z]{3})"
    r"(?:-(?P<rest>.+))?$"
)


@dataclass(frozen=True)
class ParsedTicker:
    series: str
    event_code: str  # e.g. "26APR18ATLNYK" — joins to reference game_id mapping
    away: str
    home: str
    yes_is_home: bool | None  # None for non-GAME series (YES side is not a team)


def parse_nba_ticker(ticker: str) -> ParsedTicker | None:
    """Parse an NBA market ticker. Returns None if the shape is unrecognized."""
    m = _GAME.match(ticker)
    if m:
        yes_team = m["yes_team"]
        if yes_team not in (m["away"], m["home"]):
            return None  # malformed: YES side is neither team
        return ParsedTicker(
            series=m["series"],
            event_code=f"{m['date']}{m['away']}{m['home']}",
            away=m["away"],
            home=m["home"],
            yes_is_home=(yes_team == m["home"]),
        )
    m = _ANY.match(ticker)
    if m:
        return ParsedTicker(
            series=m["series"],
            event_code=f"{m['date']}{m['away']}{m['home']}",
            away=m["away"],
            home=m["home"],
            yes_is_home=None,
        )
    return None
