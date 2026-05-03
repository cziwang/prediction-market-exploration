"""Backtest metrics: Brier score, calibration, PnL simulation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from nba_edge.backtest.engine import GameBacktestResult, TradeWithEdge


@dataclass
class PnLResult:
    """PnL simulation result for a given edge threshold."""
    edge_threshold: float
    n_trades_taken: int
    n_trades_total: int
    gross_pnl_cents: int       # total PnL in cents
    n_wins: int
    n_losses: int
    win_rate: float
    avg_edge_taken: float      # mean |edge| on trades we took


def brier_score(probs: list[float], outcomes: list[bool]) -> float:
    """Compute Brier score: mean((prob - outcome)^2). Lower = better."""
    if not probs:
        return float("nan")
    p = np.array(probs)
    o = np.array(outcomes, dtype=float)
    return float(np.mean((p - o) ** 2))


def compute_brier_scores(results: list[GameBacktestResult]) -> dict:
    """Compute Brier score for model vs market across all games.

    Returns dict with 'model_brier' and 'market_brier'.
    """
    model_probs = []
    market_probs = []
    outcomes = []

    for game in results:
        for t in game.trades:
            model_probs.append(t.model_prob_yes)
            market_probs.append(t.market_implied)
            outcomes.append(game.outcome_yes)

    return {
        "model_brier": brier_score(model_probs, outcomes),
        "market_brier": brier_score(market_probs, outcomes),
        "n_observations": len(outcomes),
    }


def calibration_bins(
    probs: list[float], outcomes: list[bool], n_bins: int = 10
) -> list[dict]:
    """Bin predictions and compute actual frequency per bin.

    Returns list of {bin_center, predicted_mean, actual_mean, count}.
    """
    p = np.array(probs)
    o = np.array(outcomes, dtype=float)
    bins = np.linspace(0, 1, n_bins + 1)
    result = []
    for i in range(n_bins):
        mask = (p >= bins[i]) & (p < bins[i + 1])
        if i == n_bins - 1:  # include right edge for last bin
            mask = (p >= bins[i]) & (p <= bins[i + 1])
        count = mask.sum()
        if count > 0:
            result.append({
                "bin_center": (bins[i] + bins[i + 1]) / 2,
                "predicted_mean": float(p[mask].mean()),
                "actual_mean": float(o[mask].mean()),
                "count": int(count),
            })
    return result


def simulate_pnl(
    results: list[GameBacktestResult],
    edge_threshold: float = 0.05,
) -> PnLResult:
    """Simulate PnL: buy YES when edge > threshold, buy NO when edge < -threshold.

    Assumes we trade 1 contract at the observed trade price.
    PnL per contract: +100-price if we buy YES and outcome is YES,
                      -price if we buy YES and outcome is NO.
    """
    total_pnl = 0
    n_taken = 0
    n_wins = 0
    n_losses = 0
    edges_taken = []
    n_total = 0

    for game in results:
        for t in game.trades:
            n_total += 1
            if t.edge > edge_threshold:
                # Buy YES
                n_taken += 1
                edges_taken.append(t.edge)
                if game.outcome_yes:
                    total_pnl += (100 - t.trade_price)
                    n_wins += 1
                else:
                    total_pnl -= t.trade_price
                    n_losses += 1
            elif t.edge < -edge_threshold:
                # Buy NO (equivalent to selling YES)
                n_taken += 1
                edges_taken.append(abs(t.edge))
                if not game.outcome_yes:
                    total_pnl += t.trade_price
                    n_wins += 1
                else:
                    total_pnl -= (100 - t.trade_price)
                    n_losses += 1

    win_rate = n_wins / n_taken if n_taken > 0 else 0.0
    avg_edge = float(np.mean(edges_taken)) if edges_taken else 0.0

    return PnLResult(
        edge_threshold=edge_threshold,
        n_trades_taken=n_taken,
        n_trades_total=n_total,
        gross_pnl_cents=total_pnl,
        n_wins=n_wins,
        n_losses=n_losses,
        win_rate=win_rate,
        avg_edge_taken=avg_edge,
    )
