# Notebooks

## Data Exploration

| # | Notebook | Purpose |
|---|----------|---------|
| 01 | `01_market_coverage.ipynb` | Parse all tickers, categorize by series type, volume by market type, identify GAME markets as primary target |
| 02 | `02_boxscore_eda.ipynb` | Understand boxscore snapshot frequency, game clock parsing, score evolution patterns |
| 03 | `explore_trades.ipynb` | [EXISTS] Trade data quality — nulls, timestamps, duplicates, latency |

## Model Development

| # | Notebook | Purpose |
|---|----------|---------|
| 10 | `10_analytical_model_calibration.ipynb` | Fit variance_rate from boxscore data, validate against known NBA value (~0.44) |
| 11 | `11_model_vs_market_overlay.ipynb` | Pick completed games, overlay model P(win) on Kalshi trade price chart — visual sanity check |
| 12 | `12_model_edge_cases.ipynb` | Test model behavior: overtime games, blowouts, close games, 4th quarter comebacks |

## Backtesting

| # | Notebook | Purpose |
|---|----------|---------|
| 20 | `20_single_game_backtest.ipynb` | End-to-end backtest on one game: align trades, compute edge, simulate PnL |
| 21 | `21_full_backtest.ipynb` | Run backtest across all completed GAME markets, aggregate stats |
| 22 | `22_edge_analysis.ipynb` | Where do edges appear? By time-in-game, score diff, game situation |
| 23 | `23_pnl_sensitivity.ipynb` | PnL as function of edge threshold, position sizing, max exposure |

## Validation

| # | Notebook | Purpose |
|---|----------|---------|
| 30 | `30_brier_score.ipynb` | Model vs market Brier score — is our model more accurate than the crowd? |
| 31 | `31_calibration.ipynb` | Reliability diagram: predicted prob vs actual outcome frequency |
| 32 | `32_information_delay.ipynb` | How stale is our boxscore data vs market reaction? Measure lag between score change and price move |

## Future (ML)

| # | Notebook | Purpose |
|---|----------|---------|
| 40 | `40_feature_engineering.ipynb` | Build features from boxscore: momentum, foul trouble, lineup changes |
| 41 | `41_ml_model_training.ipynb` | Train XGBoost/logistic on completed games, cross-validate |
| 42 | `42_ml_vs_analytical.ipynb` | Compare ML model Brier score / PnL against analytical baseline |
