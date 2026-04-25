# Product Review: MM Trading Dashboard (v2)

## Problem

The strategy runs on EC2. Monitoring requires SSH + CLI commands that take 5-10s and show 60s-stale data. Traders cannot see live market context, react to adverse moves, or intervene when the strategy accumulates bad positions.

## Goal

A real-time local dashboard that:
- Streams live order book and trade data via Kalshi WebSocket
- Shows current positions, entry prices, live mid, and mark-to-market P&L
- Allows traders to manually place/cancel orders, flatten positions, and kill the strategy
- Sub-second latency on market data updates

## Architecture

```
Your laptop
  streamlit run scripts/dashboard.py
       |
       |-- Kalshi WS (authenticated) --> orderbook_delta, trade
       |       Real-time book state, fills, trade stream
       |
       |-- Kalshi REST (authenticated) --> place/cancel orders
       |       Manual trading actions
       |
       +-- S3 (boto3) --> MMFillEvent parquet (30s cache)
               Historical fills for P&L computation
```

The dashboard connects directly to Kalshi's WebSocket with your API keys. Independent of EC2 — works even if the strategy is down.

## Data Flow

| Data | Source | Latency | Update method |
|------|--------|---------|---------------|
| Order book (bid/ask/depth) | Kalshi WS `orderbook_delta` | <100ms | Push via snapshot + delta |
| Market trades | Kalshi WS `trade` | <100ms | Push |
| Strategy fills | S3 silver `MMFillEvent` | 60s | Poll + cache |
| Strategy positions | S3 silver (computed from fills) | 60s | Poll + cache |
| Manual order placement | Kalshi REST | ~200ms | On user action |
| Manual order cancellation | Kalshi REST | ~200ms | On user action |

## Views

### 1. Overview (top bar, always visible)

```
LIVE  |  Positions: 21 tickers, 40 abs, -10 net  |  P&L: +$6.76 realized, +$1.29 MTM  |  [KILL ALL]
```

- Green "LIVE" indicator pulses on each WS message
- Red "KILL ALL" button cancels all resting orders via REST

### 2. Positions table (main view)

Every open position with live market data from WS:

```
Ticker                     Pos  Entry   Bid  Ask  Mid  Spread  MTM P&L  [Actions]
PHXDBROOKS3-25              -6    54c   98c  99c  98c     1c    -268c   [Flatten]
OKCSGA2-40                  -3    92c   96c  98c  97c     2c     -17c   [Flatten]
NYKKTOWNS32-20              +2    54c   64c  66c  65c     2c     +20c   [Flatten]
```

- Bid/Ask/Mid update in real-time from WS orderbook_delta
- MTM P&L recomputes on every mid change
- Color-coded: green winning, red losing
- "Flatten" button sends market order to close the position
- Sortable by P&L, position size, spread, or ticker

### 3. Trade panel (sidebar or modal)

Manual order entry:

```
Ticker: [KXNBAPTS-26APR25OKCPHX-PHXDBROOKS3-25  v]
Side:   [Buy YES]  [Sell YES]
Price:  [___] cents
Size:   [___] contracts
        [Place Order]
```

- Ticker dropdown pre-populated with tickers we have positions on
- Shows current bid/ask next to the price input for reference
- Confirmation before placing

### 4. Order book (for selected ticker)

When you click a ticker in the positions table:

```
PHXDBROOKS3-25     Pos: -6 @ 54c avg
YES BID          |  YES ASK
  97c  x 12      |   99c  x 8
  96c  x 5       |   100c x 3
  95c  x 20      |
```

- Full depth from WS orderbook_snapshot + deltas
- Our resting orders highlighted (if any)

### 5. Trade feed (bottom panel)

Live trade stream from WS:

```
22:17:58  SELL  ATLNALEXANDERWALKER7-20  @54c  size=3
22:17:49  BUY   NYKJBRUNSON11-25         @70c  size=1  ** OUR FILL **
22:17:45  SELL  NYKJBRUNSON11-25         @69c  size=2
```

- All trades on subscribed tickers in real-time
- Our fills highlighted (matched against known positions)

### 6. Resting orders

```
Ticker                     Side   Price  Size  Status   [Cancel]
PHXDBROOKS3-25             BID    53c    1     Resting  [X]
OKCSGA2-40                 ASK    93c    1     Resting  [X]
```

- Shows orders placed by the strategy (from WS `user_orders` if authenticated)
- Cancel button per order

## Trading Actions

| Action | Method | Details |
|--------|--------|---------|
| **Kill all** | REST `cancel_all()` | Cancel every resting order across all tickers |
| **Flatten position** | REST `place_limit(crossing)` | Place aggressive limit order to close position at market |
| **Place order** | REST `place_limit()` | Manual limit order at specified price/size |
| **Cancel order** | REST `cancel(order_id)` | Cancel a specific resting order |

All actions require confirmation dialog before execution.

## Tech Stack

**Streamlit** for the UI:
- Python-native, no JS build step
- `st.dataframe` for tables with conditional formatting
- `st.sidebar` for trade panel
- `streamlit-autorefresh` or background thread for WS data

**Threading model:**
- Main thread: Streamlit UI rendering
- Background thread: Kalshi WS connection, maintains in-memory book state
- Shared state: `threading.Lock`-protected dicts for books and trades
- REST calls: synchronous on user action (button click)

## Book State Management

The dashboard maintains its own `OrderBookState` per subscribed ticker (reuse `app/transforms/kalshi_ws.py::OrderBookState`):

```python
# Background WS thread
books: dict[str, OrderBookState] = {}  # ticker -> live book
recent_trades: deque[TradeEvent] = deque(maxlen=500)

# On orderbook_snapshot: books[ticker] = OrderBookState.from_snapshot(msg)
# On orderbook_delta: books[ticker].apply_delta(msg)
# On trade: recent_trades.append(TradeEvent(...))
```

The Streamlit UI reads from these dicts on each render cycle.

## WS Subscription

Subscribe to the same channels as the EC2 ingester but only for KXNBAPTS:

```python
channels = ["orderbook_delta", "trade"]
series = ["KXNBAPTS"]
# Subscribe to all open KXNBAPTS markets via REST lookup first
```

Optionally subscribe to `fill` and `user_orders` (private channels) if the dashboard uses the same API key as the strategy — this would show strategy orders/fills in real-time without S3 polling.

## P&L Computation

- **Strategy fills:** Loaded from S3 silver `MMFillEvent` on startup, refreshed every 30s
- **Round-trip P&L:** FIFO pairing (reuse `mm_stats.py` logic)
- **Positions:** Computed from fill deltas (reuse `_true_position()`)
- **MTM P&L:** `(mid - entry) * pos` for longs, `(entry - mid) * pos` for shorts, updated in real-time from WS mid

## File Structure

```
scripts/
  dashboard.py              # Streamlit app entry point
  dashboard/
    ws_client.py            # Background WS thread, book state management
    data.py                 # S3 data loading, P&L computation (reuse mm_stats logic)
    trading.py              # REST order placement/cancellation
```

## Dependencies

Add to `requirements.txt`:
```
streamlit>=1.30
```

`websockets` and `kalshi-python` already installed.

## MVP Scope

**v1 (build first):**
1. WS connection + live book state for KXNBAPTS tickers
2. Positions table with live bid/ask/mid from WS
3. Trade feed (live trades)
4. Kill-all button

**v2 (add after v1 works):**
5. Manual order placement (trade panel)
6. Flatten position button
7. Order book depth view
8. Resting orders display + cancel

**v3 (later):**
9. P&L charts over time
10. Player-level P&L aggregation
11. Adverse selection metrics
12. Private WS channels (fill, user_orders) for real-time strategy fill display

## Run

```bash
pip install streamlit
streamlit run scripts/dashboard.py
# Opens browser at localhost:8501
# Connects to Kalshi WS automatically
# Uses .env for KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH
```

## Security

- API keys read from `.env` (same as strategy)
- REST calls authenticated via RSA-PSS signatures (same as strategy)
- Dashboard is local-only (localhost:8501) — not exposed to internet
- All trading actions require confirmation dialog
