# Product Review: MM Trading Dashboard

## Problem

The strategy runs on EC2. To understand what's happening, you SSH in and run `python -m scripts.mm_stats`, which loads all silver data from S3, computes P&L, and prints text. This has three problems:

1. **Not real-time.** Silver data flushes every 60s. You're always looking at stale data.
2. **No market context.** You see positions and entry prices but not the current order book — can't tell if a position is in trouble until it's too late.
3. **Manual and slow.** Every check is a CLI command that takes 5-10s to load from S3.

## Goal

A local web dashboard you run on your laptop that shows:
- What positions you hold right now
- What the current market looks like for each position (bid/ask/mid/spread)
- How much you're up or down (realized + mark-to-market)
- Recent fills as they happen
- Whether the strategy is actively quoting

## Data Sources

| Data | Source | Latency | Method |
|------|--------|---------|--------|
| Current positions | Kalshi REST `get_positions()` | Real-time | Poll every 10s |
| Current order book per ticker | Kalshi REST `get_market(ticker)` | Real-time | Poll every 10s |
| Recent fills | S3 silver `MMFillEvent` | 60s | Poll on page load + refresh |
| Quoting activity | S3 silver `MMQuoteEvent` | 60s | Poll on page load |
| Round-trip P&L | Computed from fills | 60s | Recompute on refresh |

The dashboard does NOT tap the live WS stream. It reads from S3 (for fills/quotes) and Kalshi REST (for current market state). This means it works from anywhere with AWS + Kalshi credentials — no SSH needed.

## Views

### 1. Overview (home)

One-glance health check:

```
Strategy Status: ACTIVE (last quote 2m ago)
Session P&L:     +$6.76 realized, +$1.29 MTM = $8.05 est. total
Positions:       21 tickers, 40 contracts abs, -10 net
Fills today:     122 (56 buys, 66 sells, 41 round-trips)
Win rate:        71%
```

### 2. Positions table

Every open position with market context:

```
Ticker                          Pos  Entry   Bid  Ask  Mid  Spread  MTM P&L
PHXDBROOKS3-25                   -6    54c   98c  99c  98c     1c    -268c
OKCSGILGEOUSALEXANDER2-40        -3    92c   96c  98c  97c     2c     -17c
NYKKTOWNS32-20                   +2    54c   64c  66c  65c     2c     +20c
```

Color-coded: green for winning, red for losing. Sortable by P&L, position size, or spread.

### 3. Ticker detail (click into a position)

For a single ticker:
- Full order book (all price levels, not just top)
- Our position + entry price + current mid + unrealized P&L
- Fill history on this ticker
- Recent market trades from TradeEvent silver data

### 4. Fills feed

Reverse-chronological list of all fills today:

```
22:17:49  BUY   NYKJBRUNSON11-25  @70c  pos: -1->0  (closed RT, +4c)
22:14:26  SELL  NYKMBRIDGES25-15  @58c  pos: 0->-1
22:14:13  BUY   NYKMBRIDGES25-10  @56c  pos: 0->+1
```

Auto-refreshes every 30s.

### 5. P&L breakdown

- Round-trip P&L table (each completed buy-sell pair)
- Settled positions P&L
- Unsettled MTM P&L
- P&L by player (aggregate across thresholds)
- P&L over time chart

## Tech Stack

**Streamlit.** Python-native, no frontend build step, built-in auto-refresh and tables.

## Architecture

```
Your laptop
  streamlit run scripts/dashboard.py
       |
       |-- S3 (boto3) --> MMFillEvent, MMQuoteEvent parquet
       |
       +-- Kalshi REST --> get_market(ticker), positions, orderbook
```

No new infrastructure. No changes to the EC2 ingester. Dashboard is read-only.

## Reuse

- `scripts/mm_stats.py::_load_silver()` -- load parquet from S3
- `scripts/mm_stats.py::_true_position()` -- compute positions from fills
- `scripts/mm_stats.py::_lookup_markets()` -- fetch current prices from Kalshi API
- `scripts/mm_stats.py::cmd_pnl()` logic -- round-trip P&L pairing
- `app/clients/kalshi_sdk.py::make_client()` -- authenticated Kalshi client
- `app/core/config.py::SILVER_VERSION` -- filter to current data version

## MVP scope (v1)

1. Overview stats
2. Positions table with live bid/ask/mid
3. Fills feed with auto-refresh
4. P&L breakdown (round-trips + MTM)

Skip for v1: ticker detail, full order book, P&L charts, player-level aggregation.

## Refresh strategy

- **Page load:** Load all silver data for today + Kalshi REST for positions/prices
- **Auto-refresh:** Every 30s via Streamlit rerun
- **Caching:** Silver data cached 30s (`@st.cache_data(ttl=30)`), Kalshi REST always fresh

## Run

```bash
pip install streamlit
streamlit run scripts/dashboard.py
# Opens browser at localhost:8501
```
