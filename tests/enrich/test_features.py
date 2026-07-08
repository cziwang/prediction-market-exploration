"""Tests for pm.enrich.features."""

import pytest

from pm.enrich.features import edge, market_implied_probability, win_probability


class TestWinProbability:
    def test_tied_game_is_half(self) -> None:
        assert win_probability(0, 1440.0) == pytest.approx(0.5)

    def test_lead_increases_probability(self) -> None:
        assert win_probability(10, 600.0) > win_probability(5, 600.0) > 0.5

    def test_deficit_mirrors_lead(self) -> None:
        p_up = win_probability(7, 300.0)
        p_down = win_probability(-7, 300.0)
        assert p_up + p_down == pytest.approx(1.0)

    def test_same_lead_more_certain_with_less_time(self) -> None:
        assert win_probability(5, 60.0) > win_probability(5, 1200.0)

    def test_game_over_boundaries(self) -> None:
        assert win_probability(1, 0.0) == 1.0
        assert win_probability(-1, 0.0) == 0.0
        assert win_probability(0, 0.0) == 0.5

    def test_bounded(self) -> None:
        for diff in (-40, -3, 0, 3, 40):
            for secs in (1.0, 720.0, 2880.0):
                assert 0.0 <= win_probability(diff, secs) <= 1.0


class TestImpliedAndEdge:
    def test_implied(self) -> None:
        assert market_implied_probability(72) == pytest.approx(0.72)

    def test_edge_sign(self) -> None:
        assert edge(0.80, 0.72) == pytest.approx(0.08)
        assert edge(0.60, 0.72) == pytest.approx(-0.12)
