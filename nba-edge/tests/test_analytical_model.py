import pytest

from nba_edge.models.analytical import AnalyticalWinProb, parse_game_clock


class TestParseGameClock:
    def test_standard_clock(self):
        # Q1, 8:09 remaining → 3 full periods + 8:09 in current
        secs = parse_game_clock(1, "PT08M09.00S")
        assert secs == 3 * 720 + 489  # 2649

    def test_start_of_game(self):
        secs = parse_game_clock(1, "PT12M00.00S")
        assert secs == 4 * 720  # 2880

    def test_end_of_fourth(self):
        secs = parse_game_clock(4, "PT00M00.00S")
        assert secs == 0.0

    def test_third_quarter(self):
        secs = parse_game_clock(3, "PT05M30.00S")
        # 1 remaining period (4th) + 5:30
        assert secs == 720 + 330  # 1050

    def test_overtime(self):
        # OT period 5, 3:00 remaining
        secs = parse_game_clock(5, "PT03M00.00S")
        assert secs == 180.0

    def test_empty_clock(self):
        # Between periods, period 2 → 2 remaining periods
        secs = parse_game_clock(2, "")
        assert secs == 2 * 720  # 1440

    def test_empty_clock_game_over(self):
        secs = parse_game_clock(5, "")
        assert secs == 0.0


class TestAnalyticalWinProb:
    def setup_method(self):
        self.model = AnalyticalWinProb(variance_rate=0.44)

    def test_tied_at_start(self):
        # Tied game at tip-off → slightly above 50% due to home advantage
        p = self.model.predict(score_diff=0, seconds_remaining=2880)
        assert 0.5 < p < 0.6  # home advantage gives slight edge

    def test_home_leading_at_end(self):
        # Home up 5 with 10 seconds left → very high prob
        p = self.model.predict(score_diff=5, seconds_remaining=10)
        assert p > 0.95

    def test_away_leading_at_end(self):
        # Home down 10 with 30 seconds left → very low prob
        p = self.model.predict(score_diff=-10, seconds_remaining=30)
        assert p < 0.05

    def test_game_over_home_wins(self):
        p = self.model.predict(score_diff=5, seconds_remaining=0)
        assert p == 1.0

    def test_game_over_away_wins(self):
        p = self.model.predict(score_diff=-3, seconds_remaining=0)
        assert p == 0.0

    def test_game_over_tied(self):
        # Tied at end of regulation → OT, ~50/50
        p = self.model.predict(score_diff=0, seconds_remaining=0)
        assert p == 0.5

    def test_symmetry(self):
        # With no home advantage, P(home wins | +5) = 1 - P(home wins | -5)
        model_no_hca = AnalyticalWinProb(variance_rate=0.44, home_advantage=0)
        p_pos = model_no_hca.predict(score_diff=5, seconds_remaining=1000)
        p_neg = model_no_hca.predict(score_diff=-5, seconds_remaining=1000)
        assert abs(p_pos + p_neg - 1.0) < 1e-10

    def test_monotonic_in_score_diff(self):
        # Higher score diff → higher win prob
        p1 = self.model.predict(score_diff=0, seconds_remaining=1000)
        p2 = self.model.predict(score_diff=5, seconds_remaining=1000)
        p3 = self.model.predict(score_diff=10, seconds_remaining=1000)
        assert p1 < p2 < p3

    def test_monotonic_in_time(self):
        # Same lead, less time → higher confidence
        p1 = self.model.predict(score_diff=5, seconds_remaining=2000)
        p2 = self.model.predict(score_diff=5, seconds_remaining=500)
        p3 = self.model.predict(score_diff=5, seconds_remaining=60)
        assert p1 < p2 < p3

    def test_predict_from_game_state(self):
        p = self.model.predict_from_game_state(
            home_score=55, away_score=50, period=2, game_clock="PT06M00.00S"
        )
        # Home up 5 at halftime-ish → slightly above 50%
        assert 0.5 < p < 0.8
