"""NBA game state events (DESIGN.md §2)."""

from pm.core.events import MarketEvent


class GameStateEvent(MarketEvent):
    """NBA game state at a point in time, derived from one play-by-play action."""

    game_id: str
    period: int
    clock_seconds: float  # seconds remaining in current period
    seconds_remaining: float  # total seconds remaining in regulation-equivalent game
    score_home: int
    score_away: int
    score_diff: int  # score_home - score_away
    action_type: str
    t_event_ns: int | None = None  # when this action happened on court (frame.timeActual)
