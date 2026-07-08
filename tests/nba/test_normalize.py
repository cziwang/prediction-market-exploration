"""Tests for pm.nba.normalize against the golden fixture + bad input."""

import gzip
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from pm.core.dlq import Dlq, Ok
from pm.nba.normalize import normalize_nba_pbp

GOLDEN_PBP = Path(__file__).parent.parent / "fixtures" / "golden" / "nba_pbp_0042500121.jsonl.gz"


class TestGoldenPbp:
    def test_every_golden_line_normalizes(self) -> None:
        with gzip.open(GOLDEN_PBP, "rb") as f:
            results = [normalize_nba_pbp(line) for line in f if line.strip()]
        assert len(results) == 548
        dlq = [r for r in results if isinstance(r, Dlq)]
        assert dlq == [], f"unexpected DLQ: {dlq[:3]}"

    def test_first_action_fields(self) -> None:
        with gzip.open(GOLDEN_PBP, "rb") as f:
            first = normalize_nba_pbp(next(f))
        assert isinstance(first, Ok)
        gs = first.event
        assert gs.game_id == "0042500121"
        assert gs.period == 1
        assert gs.clock_seconds == 720.0  # PT12M00.00S — period start
        assert gs.seconds_remaining == 4 * 720.0  # full game ahead
        assert gs.score_home == 0 and gs.score_away == 0
        assert gs.t_event_ns is not None

    def test_final_action_is_game_end(self) -> None:
        with gzip.open(GOLDEN_PBP, "rb") as f:
            lines = [line for line in f if line.strip()]
        last = normalize_nba_pbp(lines[-1])
        assert isinstance(last, Ok)
        gs = last.event
        assert gs.score_home == 113 and gs.score_away == 102  # NYK 113-102 ATL
        assert gs.score_diff == 11
        assert gs.period == 4
        assert gs.seconds_remaining == 0.0


class TestDerivations:
    def _line(self, period: int, clock: str) -> bytes:
        import json

        return json.dumps(
            {
                "game_id": "g",
                "t_receipt": 1.0,
                "frame": {
                    "period": period,
                    "clock": clock,
                    "scoreHome": "10",
                    "scoreAway": "8",
                    "actionType": "2pt",
                    "timeActual": "2026-04-18T22:54:14.2Z",
                },
            }
        ).encode()

    def test_mid_game_seconds_remaining(self) -> None:
        r = normalize_nba_pbp(self._line(2, "PT08M21.00S"))
        assert isinstance(r, Ok)
        # 2 full periods left (Q3, Q4) + 501s of Q2
        assert r.event.seconds_remaining == 2 * 720 + 501.0

    def test_overtime_uses_clock_only(self) -> None:
        r = normalize_nba_pbp(self._line(5, "PT03M00.00S"))
        assert isinstance(r, Ok)
        assert r.event.seconds_remaining == 180.0  # no regulation periods left

    def test_bad_clock_goes_to_dlq(self) -> None:
        r = normalize_nba_pbp(self._line(2, "8:21"))
        assert isinstance(r, Dlq)
        assert "clock" in r.error


class TestBadInput:
    @given(st.binary(max_size=200))
    def test_never_raises(self, raw: bytes) -> None:
        result = normalize_nba_pbp(raw)
        assert isinstance(result, (Ok, Dlq))
