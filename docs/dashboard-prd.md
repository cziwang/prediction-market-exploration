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
       |-- Kalshi WS (single authenticated connection)
       |       Public:  orderbook_snapshot, orderbook_delta, trade
       |       Private: fill, user_orders, market_positions, market_lifecycle_v2
       |
       |-- Kalshi REST (on user action only)
       |       place_limit(), cancel(), cancel_all()
       |
       +-- S3 (optional, startup only)
               Backfill today's fills before WS connected
```

Single authenticated WS connection carries all channels (public + private coexist). Independent of EC2 — works even if the strategy is down.

## WebSocket Channels

All data comes through a single authenticated Kalshi WS connection. No S3 polling needed.

### Public channels (per-market subscription)

| Channel | What it gives you | Latency |
|---------|-------------------|---------|
| `orderbook_snapshot` | Full book on subscribe (seed state) | One-time |
| `orderbook_delta` | Incremental book updates | <100ms push |
| `trade` | Every trade on subscribed tickers | <100ms push |

### Private channels (global, your account only)

| Channel | What it gives you | Latency |
|---------|-------------------|---------|
| `fill` | Your fills with price, size, fee, post-position | <100ms push |
| `user_orders` | Order ACKs, cancellations, rejections, remaining size | <100ms push |
| `market_positions` | Net position, entry price, cost basis, realized/unrealized P&L per ticker | <100ms push |
| `market_lifecycle_v2` | Market open/close/settled status | Event-driven |

### What each private channel provides

**`fill`** — authoritative fill data:
- `order_id`, `market_ticker`, `action` (buy/sell), `count_fp` (size), `yes_price_dollars`, `fee_cost`, `is_taker`, `post_position_fp`, `ts_ms`

**`user_orders`** — order state machine:
- `order_id`, `client_order_id`, `market_ticker`, `status` (pending/resting/canceled/executed/rejected), `side`, `action`, `yes_price_dollars`, `remaining_count_fp`, `ts_ms`

**`market_positions`** — Kalshi's authoritative position view:
- `market_ticker`, `position_fp`, `entry_price_cents`, `cost_basis`, `realized_pnl`, `unrealized_pnl`

**`market_lifecycle_v2`** — market status changes:
- `market_ticker`, `event_type` (opened/activated/deactivated/determined/settled), `ts_ms`

### Data flow (zero S3 dependency)

| Data | Source | Latency |
|------|--------|---------|
| Order book (all levels) | WS `orderbook_snapshot` + `orderbook_delta` | <100ms |
| Market trades | WS `trade` | <100ms |
| Our fills | WS `fill` | <100ms |
| Our positions + entry price + P&L | WS `market_positions` | <100ms |
| Our resting orders | WS `user_orders` | <100ms |
| Market open/closed status | WS `market_lifecycle_v2` | Event-driven |
| Manual order placement | Kalshi REST | ~200ms |
| Manual order cancellation | Kalshi REST | ~200ms |

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

Live trade stream from WS `trade` channel:

```
22:17:58  SELL  ATLNALEXANDERWALKER7-20  @54c  size=3
22:17:49  BUY   NYKJBRUNSON11-25         @70c  size=1  ** OUR FILL **
22:17:45  SELL  NYKJBRUNSON11-25         @69c  size=2
```

- All trades on subscribed tickers in real-time
- Our fills highlighted using WS `fill` channel (shows fee, is_taker, post-position)

### 6. Resting orders (from WS `user_orders`)

```
Ticker                     Side   Price  Size  Remaining  Status   [Cancel]
PHXDBROOKS3-25             BID    53c    1     1          Resting  [X]
OKCSGA2-40                 ASK    93c    1     1          Resting  [X]
```

- Updated in real-time via `user_orders` push (ACK, cancel, reject, fill)
- Cancel button per order
- Shows rejected orders with error message

### 7. Market status (from WS `market_lifecycle_v2`)

- Markets that become `deactivated` are grayed out in the positions table
- Warning banner when a market is about to settle
- Disable order placement on closed markets

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
# Background WS thread — shared state (Lock-protected)
books: dict[str, OrderBookState] = {}         # ticker -> live book
recent_trades: deque[dict] = deque(maxlen=500) # trade feed
fills: list[dict] = []                         # our fills this session
positions: dict[str, dict] = {}                # ticker -> {position, entry_price, pnl, ...}
resting_orders: dict[str, dict] = {}           # order_id -> {ticker, side, price, remaining, status}
market_status: dict[str, str] = {}             # ticker -> "active"/"deactivated"/"settled"

# Public channel handlers:
# orderbook_snapshot → books[ticker] = OrderBookState.from_snapshot(msg)
# orderbook_delta   → books[ticker].apply_delta(msg)
# trade             → recent_trades.append(msg)

# Private channel handlers:
# fill              → fills.append(msg); update positions from post_position_fp
# user_orders       → resting_orders[order_id] = msg (upsert by status)
# market_positions  → positions[ticker] = msg (authoritative position + P&L)
# market_lifecycle  → market_status[ticker] = event_type
```

The Streamlit UI reads from these dicts on each render cycle (~1-2s).

## WS Subscription

Two subscriptions on one connection:

```python
# 1. Public channels — per-market, need ticker list
ws.send(json.dumps({
    "id": 1, "cmd": "subscribe",
    "params": {
        "channels": ["orderbook_delta", "trade"],
        "market_tickers": open_kxnbapts_tickers,  # from REST lookup on startup
    }
}))

# 2. Private channels — global, no ticker filter needed
ws.send(json.dumps({
    "id": 2, "cmd": "subscribe",
    "params": {
        "channels": ["fill", "user_orders", "market_positions", "market_lifecycle_v2"],
    }
}))
```

Private channels deliver data for your account across all markets — no need to specify tickers. This means you see fills from the EC2 strategy too (same API key).

## P&L Computation

All real-time, no S3 dependency:

- **Positions:** From WS `market_positions` channel — gives net position, entry price, cost basis, realized P&L, unrealized P&L per ticker. This is Kalshi's authoritative view.
- **Fills:** From WS `fill` channel — each fill includes price, size, fee, and post-position. Accumulated in-memory for session history.
- **Round-trip P&L:** FIFO pairing of accumulated fills (reuse `mm_stats.py` logic on in-memory fill list).
- **MTM P&L:** Two sources that should agree:
  - WS `market_positions` provides `unrealized_pnl` directly
  - Can also compute from `(mid - entry) * pos` using live book mid from `orderbook_delta`
- **On startup:** Optionally load today's fills from S3 silver to backfill session history before WS was connected. After that, purely WS-driven.

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
