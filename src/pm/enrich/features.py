"""Feature computation for enriched trades (DESIGN.md §3).

Pure functions — no I/O, no state. The same code runs in the Flink operator
and in any offline analysis, so model behavior is identical everywhere.
"""

import math


def win_probability(score_diff: int, seconds_remaining: float) -> float:
    """Analytical home-team win probability.

    P(home win) = Phi(score_diff / sqrt(0.44 * seconds_remaining))

    A Brownian-motion model of the score margin: the current lead is the drift
    already realized, and remaining variance scales linearly with time left
    (0.44 points^2 per second is an empirical NBA constant). As
    seconds_remaining -> 0 the probability collapses to a step function on the
    current lead.

    Boundary cases:
    - seconds_remaining <= 0: game over bar OT; return 1.0 / 0.0 / 0.5 on sign
    - score_diff == 0 with time left: exactly 0.5
    """
    if seconds_remaining <= 0:
        if score_diff > 0:
            return 1.0
        if score_diff < 0:
            return 0.0
        return 0.5
    z = score_diff / math.sqrt(0.44 * seconds_remaining)
    # Phi(z) via erf — avoids a scipy dependency
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def market_implied_probability(price_cents: int) -> float:
    """A YES contract trading at p cents implies P(YES) = p/100."""
    return price_cents / 100.0


def edge(model_prob: float, market_prob: float) -> float:
    """Positive edge: model thinks YES is more likely than the market price implies."""
    return model_prob - market_prob
