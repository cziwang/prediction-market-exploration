"""Analytical win probability model.

Models the score differential as a Brownian motion (random walk).
Given the current score difference and time remaining, computes
P(home_win) using the normal CDF.

    P(home_win) = Phi(score_diff / sqrt(variance_rate * seconds_remaining))

The key parameter `variance_rate` represents the variance of score
changes per second. For NBA games, this is approximately 0.44 pts^2/s.
"""

from __future__ import annotations

import re

from scipy.stats import norm


# NBA regulation: 4 periods of 12 minutes = 2880 seconds
REGULATION_SECONDS = 4 * 12 * 60
OT_SECONDS = 5 * 60  # 5-minute overtime periods
PERIOD_SECONDS = 12 * 60


def parse_game_clock(period: int, game_clock: str) -> float:
    """Convert period + game clock string to seconds remaining in regulation.

    Args:
        period: Current period (1-4 for regulation, 5+ for OT)
        game_clock: ISO 8601 duration like "PT08M09.00S" or "" if between periods

    Returns:
        Seconds remaining. For OT, returns seconds remaining in the OT period.
        Returns 0.0 if game is over.
    """
    if not game_clock:
        # Between periods or game over — assume period just ended
        if period <= 4:
            return max(0.0, (4 - period) * PERIOD_SECONDS)
        return 0.0

    # Parse "PT08M09.00S" format
    match = re.match(r"PT(\d+)M([\d.]+)S", game_clock)
    if not match:
        # Fallback: try just seconds
        match = re.match(r"PT([\d.]+)S", game_clock)
        if match:
            clock_seconds = float(match.group(1))
        else:
            return 0.0
    else:
        minutes = int(match.group(1))
        seconds = float(match.group(2))
        clock_seconds = minutes * 60 + seconds

    if period <= 4:
        # Regulation: time left in this period + full remaining periods
        remaining_periods = 4 - period
        return clock_seconds + remaining_periods * PERIOD_SECONDS
    else:
        # Overtime: just time left in current OT period
        return clock_seconds


class AnalyticalWinProb:
    """Win probability model based on score differential random walk.

    Args:
        variance_rate: Variance of score changes per second.
            NBA default ≈ 0.44 pts^2/s (empirical from historical data).
        home_advantage: Points of expected home team advantage over a full game.
            NBA average ~3.5 points. Set to 0 for no prior.
    """

    def __init__(self, variance_rate: float = 0.44, home_advantage: float = 3.5):
        self.variance_rate = variance_rate
        self.home_advantage = home_advantage

    def predict(self, score_diff: int, seconds_remaining: float) -> float:
        """Return P(home_win) given current state.

        The model accounts for home court advantage by adding an expected
        drift: the home team is expected to outscore the away team by
        (home_advantage * seconds_remaining / 2880) more points in the
        remaining time.

        Args:
            score_diff: home_score - away_score (positive = home leading)
            seconds_remaining: seconds left in regulation (or OT period)

        Returns:
            Probability that home team wins (0.0 to 1.0)
        """
        if seconds_remaining <= 0:
            if score_diff > 0:
                return 1.0
            elif score_diff < 0:
                return 0.0
            else:
                return 0.5  # tied at end → OT, roughly 50/50

        # Expected additional home advantage in remaining time
        expected_drift = self.home_advantage * (seconds_remaining / REGULATION_SECONDS)
        adjusted_diff = score_diff + expected_drift

        sigma = (self.variance_rate * seconds_remaining) ** 0.5
        return float(norm.cdf(adjusted_diff / sigma))

    def predict_from_game_state(
        self, home_score: int, away_score: int, period: int, game_clock: str
    ) -> float:
        """Convenience: predict directly from boxscore fields."""
        score_diff = home_score - away_score
        seconds_remaining = parse_game_clock(period, game_clock)
        return self.predict(score_diff, seconds_remaining)
