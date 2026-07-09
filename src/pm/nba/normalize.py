"""Normalize bronze NBA CDN play-by-play envelopes into GameStateEvents.

Bronze envelope shape (one JSON object per line):

    {"source": "nba_cdn", "channel": "live_pbp", "game_id": "0042500121",
     "action_number": 188, "t_receipt": <float seconds>, "poll_seq": 74,
     "frame": {"clock": "PT08M21.00S", "timeActual": "2026-04-18T22:54:14.2Z",
               "period": 2, "scoreHome": "42", "scoreAway": "34",
               "actionType": "2pt", ...}}

Derivations happen here and nowhere else:
- clock "PT08M21.00S"       -> 501.0 seconds remaining in period
- seconds_remaining          = max(0, 4 - period) * 720 + clock_seconds
  (period 5+ is overtime: only the OT clock remains)
- score_diff                 = score_home - score_away
- timeActual ISO string      -> t_event_ns (on-court time, nanoseconds)
"""

import json
import re
from datetime import datetime
from typing import Any

from pm.core.dlq import Dlq, NormalizeResult, Ok
from pm.nba.events import GameStateEvent

_CLOCK = re.compile(r"^PT(\d+)M([\d.]+)S$")
_PERIOD_SECONDS = 720.0  # 12-minute NBA quarter
_REGULATION_PERIODS = 4


def _parse_clock(clock: str) -> float:
    m = _CLOCK.match(clock)
    if not m:
        raise ValueError(f"unparseable clock: {clock!r}")
    return int(m.group(1)) * 60 + float(m.group(2))


def _seconds_remaining(period: int, clock_seconds: float) -> float:
    periods_left = max(0, _REGULATION_PERIODS - period)
    return periods_left * _PERIOD_SECONDS + clock_seconds


def _iso_to_ns(iso: str) -> int:
    # frame.timeActual: "2026-04-18T22:54:14.2Z"
    # Python 3.10's fromisoformat rejects fractional seconds with != 3 or 6
    # digits (e.g. ".6" or ".60"). Pad to 6 digits for cross-version safety.
    s = iso.replace("Z", "+00:00")
    dot = s.find(".")
    if dot != -1:
        plus = s.index("+", dot)
        frac = s[dot + 1 : plus]
        s = s[: dot + 1] + frac.ljust(6, "0") + s[plus:]
    dt = datetime.fromisoformat(s)
    return int(dt.timestamp() * 1_000_000_000)


def normalize_nba_pbp(raw: bytes, context: str = "nba_cdn") -> NormalizeResult:
    """Normalize one bronze PBP line. Never raises; bad input -> Dlq."""
    try:
        record: dict[str, Any] = json.loads(raw)
        frame = record["frame"]
        period = int(frame["period"])
        clock_seconds = _parse_clock(frame["clock"])

        t_event_ns: int | None = None
        time_actual = frame.get("timeActual")
        if time_actual:
            t_event_ns = _iso_to_ns(time_actual)

        return Ok(
            GameStateEvent(
                t_receipt_ns=int(record["t_receipt"] * 1_000_000_000),
                source="nba_cdn",
                game_id=record["game_id"],
                period=period,
                clock_seconds=clock_seconds,
                seconds_remaining=_seconds_remaining(period, clock_seconds),
                score_home=int(frame["scoreHome"]),
                score_away=int(frame["scoreAway"]),
                score_diff=int(frame["scoreHome"]) - int(frame["scoreAway"]),
                action_type=frame["actionType"],
                t_event_ns=t_event_ns,
            )
        )
    except Exception as exc:  # noqa: BLE001 — boundary: any bad input becomes Dlq
        return Dlq(raw, f"{type(exc).__name__}: {exc}", context)
