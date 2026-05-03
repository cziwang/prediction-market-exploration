"""Backtest engine: replay trades, compute model edge, simulate PnL."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from nba_edge.data.game_alignment import AlignedTrade, align_trades_with_game
from nba_edge.data.s3_reader import load_boxscores_for_game
from nba_edge.data.ticker_parser import parse_ticker
from nba_edge.models.analytical import AnalyticalWinProb


@dataclass
class TradeWithEdge:
    """A trade annotated with model probability and edge."""
    t_receipt_ns: int
    market_ticker: str
    trade_price: int
    trade_side: str
    trade_size: int
    score_diff: int
    seconds_remaining: float
    period: int
    model_prob_yes: float     # model's P(yes outcome)
    market_implied: float     # trade_price / 100
    edge: float               # model_prob_yes - market_implied


@dataclass
class GameBacktestResult:
    """Backtest result for a single game/market."""
    market_ticker: str
    game_id: str
    yes_team: str
    home_team: str
    away_team: str
    outcome_yes: bool         # did "yes" win?
    final_home_score: int
    final_away_score: int
    trades: list[TradeWithEdge]


class BacktestEngine:
    def __init__(self, model: AnalyticalWinProb | None = None,
                 max_seconds_remaining: float = 2400):
        """
        Args:
            model: Win probability model
            max_seconds_remaining: Only consider trades with less time remaining.
                Default 2400s (= skip first ~8 min). The analytical model can't
                beat the market early because it doesn't know team strength.
        """
        self.model = model or AnalyticalWinProb()
        self.max_seconds_remaining = max_seconds_remaining

    def run_game(
        self,
        trades_df: pl.DataFrame,
        boxscore_snapshots: list[dict],
        market_ticker: str,
    ) -> GameBacktestResult | None:
        """Run backtest for one market ticker on one game.

        Args:
            trades_df: trades filtered to this market ticker
            boxscore_snapshots: sorted boxscore snapshots for the game
            market_ticker: the Kalshi ticker

        Returns:
            GameBacktestResult or None if alignment fails
        """
        if not boxscore_snapshots or trades_df.is_empty():
            return None

        parsed = parse_ticker(market_ticker)
        home_team = boxscore_snapshots[0]["home_team"]
        away_team = boxscore_snapshots[0]["away_team"]
        yes_team = parsed.selection  # e.g. "MIN" from "KXNBAGAME-...-MIN"

        # Determine if yes_team is home or away
        yes_is_home = (yes_team == home_team)

        # Align trades with game state
        aligned = align_trades_with_game(
            trades_df, boxscore_snapshots, yes_team, home_team
        )
        if not aligned:
            return None

        # Determine game outcome from final snapshot
        final = boxscore_snapshots[-1]
        if yes_is_home:
            outcome_yes = final["home_score"] > final["away_score"]
        else:
            outcome_yes = final["away_score"] > final["home_score"]

        # Compute model probability and edge for each trade
        trades_with_edge = []
        for t in aligned:
            # Skip trades too early in the game (model can't beat market there)
            if t.seconds_remaining > self.max_seconds_remaining:
                continue

            # Model gives P(home_win)
            p_home = self.model.predict(t.score_diff, t.seconds_remaining)
            # Convert to P(yes_team_win)
            model_prob_yes = p_home if yes_is_home else (1.0 - p_home)
            market_implied = t.trade_price / 100.0
            edge = model_prob_yes - market_implied

            trades_with_edge.append(TradeWithEdge(
                t_receipt_ns=t.t_receipt_ns,
                market_ticker=t.market_ticker,
                trade_price=t.trade_price,
                trade_side=t.trade_side,
                trade_size=t.trade_size,
                score_diff=t.score_diff,
                seconds_remaining=t.seconds_remaining,
                period=t.period,
                model_prob_yes=model_prob_yes,
                market_implied=market_implied,
                edge=edge,
            ))

        return GameBacktestResult(
            market_ticker=market_ticker,
            game_id="",  # filled by caller if known
            yes_team=yes_team or "",
            home_team=home_team,
            away_team=away_team,
            outcome_yes=outcome_yes,
            final_home_score=final["home_score"],
            final_away_score=final["away_score"],
            trades=trades_with_edge,
        )
