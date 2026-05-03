# nba-edge — NBA Prediction Model + Edge Trading

Build a predictive model for NBA game outcomes, compare against Kalshi market prices, trade when we have edge.

## Architecture

- `nba_edge/data/` — S3 readers, ticker parsing, game-to-trade alignment
- `nba_edge/models/` — Win probability models (analytical baseline, future ML)
- `nba_edge/backtest/` — Replay engine, PnL simulation, Brier score / calibration
- `notebooks/` — EDA, model development, backtest results
- `tests/` — Unit tests for parser + model

## Key concepts

- **Analytical model**: P(home_win) = Phi(score_diff / sqrt(0.44 * seconds_remaining))
- **Edge**: model_prob - market_implied_prob. Trade when |edge| > threshold.
- **As-of join**: align each Kalshi trade with the most recent boxscore snapshot

## Data sources (all in S3 bucket `prediction-markets-data`)

- Silver TradeEvent: `silver/kalshi_ws/TradeEvent/date=YYYY-MM-DD/v=3/`
- Bronze boxscores: `bronze/nba_cdn/boxscore/YYYY/MM/DD/HH/*.jsonl.gz`

## Commands

```bash
source .venv/bin/activate
python -m pytest nba-edge/tests/ -v
```
