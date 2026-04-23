# Strategy: Passive Market Making on Kalshi NBA Player Props

## Goal

Run a live market maker on Kalshi KXNBAPTS (player points) contracts, posting limit orders at the best bid and ask when spreads are wide enough to cover fees and adverse selection. The edge is structural — hundreds of thin prop markets with $0.03–$0.10 spreads and low adverse selection — not a speed game.

## Why KXNBAPTS

The MM simulation (`notebooks/strategies/market_making.ipynb`) tested four series. Only KXNBAPTS has durable edge:

| Series | Median spread | Edge/fill (10s) | Win rate (RT) | Survives 1s latency? |
|---|---|---|---|---|
| **KXNBAPTS** | **$0.04** | **$0.034** | **85%** | **Yes** |
| KXNBAGAME | $0.01 | $0.002 | 40% | Marginal |
| KXNBASPREAD | $0.02 | $0.005 | 44% | Marginal |
| KXNBATOTAL | $0.01 | $0.005 | 84% | Yes, but thin |

KXNBAPTS works because of structural fragmentation: hundreds of player/game/threshold combinations spread market-maker attention thin, leaving wide spreads. Adverse selection is low because player props move slowly (one basket at a time) unlike game-winner markets that swing on a single play.

## Architecture

The strategy plugs into the existing live pipeline defined in [`docs/data-flow.md`](data-flow.md). It runs inline in the Kalshi WS live process — no new process, no new infra.

```
┌─────────────────────────────────────────────────────────────────┐
│  scripts/live/kalshi_ws (existing process)                      │
│                                                                 │
│    WS ingester (existing)                                       │
│         │                                                       │
│         │  raw frame                                            │
│         ├──────────▶  BronzeWriter  (existing)                  │
│         │                                                       │
│         ▼                                                       │
│    transform()  (NEW — app/transforms/kalshi_ws.py)             │
│         │                                                       │
│         │  OrderBookUpdate | TradeEvent                         │
│         ├──────────▶  MMStrategy.on_event()  (NEW)              │
│         │                   │                                   │
│         │                   │  Place/cancel limit orders        │
│         │                   ▼                                   │
│         │              Kalshi REST API                          │
│         │                                                       │
│         └──────────▶  SilverWriter  (existing, not yet wired)   │
└─────────────────────────────────────────────────────────────────┘
```

### What exists today

- **Ingester** (`scripts/live/kalshi_ws/__main__.py`): connects to WS, subscribes to `orderbook_delta` + `trade` for KXNBAGAME/KXNBASPREAD/KXNBATOTAL/KXNBAPTS, archives raw frames to bronze. Working, deployed.
- **BronzeWriter** (`app/services/bronze_writer.py`): async batched gzip-JSONL to S3. Working.
- **SilverWriter** (`app/services/silver_writer.py`): async batched Parquet to S3. Working but not wired into the live process.
- **Event types** (`app/events.py`): `OrderBookUpdate`, `TradeEvent`. Defined, not yet produced by the live process.

### What we build

Four new components, in dependency order:

```
app/
├── transforms/
│   └── kalshi_ws.py          # NEW: raw frame → OrderBookUpdate | TradeEvent
├── strategy/
│   └── mm.py                 # NEW: the market making strategy
├── clients/
│   └── kalshi_rest_orders.py # NEW: place/cancel limit orders via REST
└── events.py                 # EXTEND: add MMFill, MMQuote events for logging
```

## Component design

### 1. Transform: `app/transforms/kalshi_ws.py`

Stateful transform that maintains per-ticker order book state and emits typed events. Called synchronously on the ingester's task per `data-flow.md` rules.

```python
class KalshiTransform:
    """Raw WS frame → typed Event. Maintains order book state."""
    
    def __init__(self):
        self._books: dict[str, OrderBookState] = {}
    
    def __call__(self, frame: dict, t_receipt: float) -> Event | None:
        msg_type = frame.get("type")
        
        if msg_type == "orderbook_snapshot":
            ticker = frame["msg"]["market_ticker"]
            self._books[ticker] = OrderBookState.from_snapshot(frame["msg"])
            book = self._books[ticker]
            return OrderBookUpdate(
                t_receipt=t_receipt,
                market_ticker=ticker,
                bid_yes=book.best_bid,
                ask_yes=book.best_ask,
                bid_size=book.bid_size_top,
                ask_size=book.ask_size_top,
            )
        
        if msg_type == "orderbook_delta":
            ticker = frame["msg"]["market_ticker"]
            book = self._books.get(ticker)
            if book is None:
                return None  # delta without snapshot — skip
            book.apply_delta(frame["msg"])
            return OrderBookUpdate(
                t_receipt=t_receipt,
                market_ticker=ticker,
                bid_yes=book.best_bid,
                ask_yes=book.best_ask,
                bid_size=book.bid_size_top,
                ask_size=book.ask_size_top,
            )
        
        if msg_type == "trade":
            msg = frame["msg"]
            return TradeEvent(
                t_receipt=t_receipt,
                market_ticker=msg["market_ticker"],
                side=msg["taker_side"],       # "yes" or "no"
                price=int(msg["yes_price"]),   # cents
                size=int(msg["count"]),
            )
        
        return None  # control frames (ok, subscribed, etc.)
```

`OrderBookState` is a helper class that holds the current YES and NO books as `dict[int, int]` (price cents → size cents), applies deltas, and exposes `best_bid`, `best_ask`, `spread`, `mid`. Uses integer cents throughout — no floats for prices.

### 2. Strategy: `app/strategy/mm.py`

The core quoting logic. Receives `OrderBookUpdate` and `TradeEvent`, decides when to post/cancel orders. Must be non-blocking per `data-flow.md` rules — order placement is fire-and-forget via an async queue.

```python
class MMStrategy:
    """Passive market maker on KXNBAPTS."""
    
    def __init__(self, order_client, config: MMConfig):
        self._client = order_client
        self._config = config
        self._positions: dict[str, int] = {}       # ticker → net contracts
        self._live_orders: dict[str, OrderPair] = {} # ticker → (bid_id, ask_id)
        self._order_queue: asyncio.Queue = asyncio.Queue()
    
    def on_event(self, event: Event) -> None:
        if isinstance(event, OrderBookUpdate):
            self._on_book_update(event)
        elif isinstance(event, TradeEvent):
            self._on_trade(event)
    
    def _on_book_update(self, update: OrderBookUpdate) -> None:
        ticker = update.market_ticker
        
        # Only trade KXNBAPTS
        if not ticker.startswith("KXNBAPTS-"):
            return
        
        spread = update.ask_yes - update.bid_yes
        position = self._positions.get(ticker, 0)
        
        # --- Quoting decision ---
        should_bid = (
            spread >= self._config.min_spread_cents
            and position < self._config.max_position   # don't buy more if too long
        )
        should_ask = (
            spread >= self._config.min_spread_cents
            and position > -self._config.max_position  # don't sell more if too short
        )
        
        # Skew quotes when carrying inventory
        bid_price = update.bid_yes
        ask_price = update.ask_yes
        if position > self._config.skew_threshold:
            # We're long — widen bid (less eager to buy more)
            bid_price = max(1, bid_price - 1)
        elif position < -self._config.skew_threshold:
            # We're short — widen ask (less eager to sell more)
            ask_price = min(99, ask_price + 1)
        
        self._update_quotes(ticker, bid_price if should_bid else None,
                                     ask_price if should_ask else None)
    
    def _on_trade(self, trade: TradeEvent) -> None:
        """Detect fills on our resting orders (by matching against live_orders)."""
        ticker = trade.market_ticker
        orders = self._live_orders.get(ticker)
        if orders is None:
            return
        
        # Check if this trade filled our bid or ask
        # (In production, use Kalshi's fill notifications instead of inference)
        ...
```

#### Configuration: `MMConfig`

```python
@dataclass
class MMConfig:
    min_spread_cents: int = 3       # don't quote if spread < 3c
    max_position: int = 10          # per-ticker position limit (contracts)
    skew_threshold: int = 3         # start skewing quotes at this position
    flatten_before_settle_s: int = 300  # cancel all orders 5 min before settlement
    order_size: int = 1             # contracts per side
```

Start conservative: 1 contract per side, 10-contract position limit, $0.03 minimum spread. Increase sizing after live validation.

### 3. Order client: `app/clients/kalshi_rest_orders.py`

Thin wrapper around Kalshi's REST API for order management. The existing `kalshi_sdk.py` uses the official SDK for market queries; this adds order placement.

```python
class KalshiOrderClient:
    """Place and cancel limit orders on Kalshi via REST."""
    
    async def place_limit(self, ticker: str, side: str, price_cents: int,
                          size: int) -> str:
        """Returns order_id."""
        ...
    
    async def cancel(self, order_id: str) -> None:
        ...
    
    async def cancel_all(self, ticker: str) -> None:
        """Cancel all resting orders on a ticker."""
        ...
    
    async def get_positions(self) -> dict[str, int]:
        """Fetch current positions from Kalshi (source of truth)."""
        ...
```

Key design choice: **order placement is async and fire-and-forget from the strategy's perspective.** The strategy pushes order intents onto an `asyncio.Queue`; a background task drains the queue and calls Kalshi's REST API. This keeps `on_event()` non-blocking per `data-flow.md` rules.

### 4. Event extensions: `app/events.py`

Add logging events so silver captures strategy activity:

```python
@dataclass(frozen=True)
class MMQuoteEvent:
    t_receipt: float
    market_ticker: str
    bid_price: int | None   # cents, or None if not quoting bid
    ask_price: int | None
    position: int           # current net position on this ticker
    spread: int             # spread at time of quote decision

@dataclass(frozen=True)
class MMFillEvent:
    t_receipt: float
    market_ticker: str
    side: str               # "buy" or "sell"
    price: int              # cents
    size: int
    position_after: int     # net position after this fill
    maker_fee: int          # cents
```

These flow through SilverWriter → Parquet → notebooks for post-hoc analysis of live strategy performance.

## Wiring into the live process

The ingester's main loop (`scripts/live/kalshi_ws/__main__.py`) changes from bronze-only to bronze + transform + strategy + silver:

```python
# Current (bronze only):
async for raw in ws:
    await self._archive(raw)

# New (bronze + transform + strategy + silver):
async for raw in ws:
    await self._archive(raw)           # bronze (unchanged)
    
    frame = json.loads(raw)
    event = self._transform(frame, time.time())
    if event is None:
        continue
    
    self._strategy.on_event(event)     # quoting decisions (non-blocking)
    await self._silver.emit(event)     # typed events to Parquet
```

This matches the architecture in `data-flow.md` exactly — the transform runs inline, its output feeds both strategy and silver simultaneously.

## Risk management

### Position limits

The `max_position` config caps per-ticker exposure. When hit, the strategy stops posting on the side that would increase exposure.

From the simulation: NBA bettors buy YES 3:1 over NO, so the strategy accumulates short inventory. Without limits, the simulation hit -1,100 contracts of open inventory. With `max_position=10`, the worst-case per-ticker exposure is $10 (10 contracts × $1 settlement).

### Pre-settlement flattening

Binary contracts settle at $0 or $1 at game end. Open positions at settlement are a coin flip, not a spread trade.

`flatten_before_settle_s = 300`: five minutes before expected game end, cancel all orders and place market orders to flatten any remaining inventory. The cost of crossing the spread to flatten (~$0.02/contract) is insurance against the ~$0.50 expected settlement cost of an unhedged position.

Implementation: the strategy needs game-end estimates. Source: NBA CDN scoreboard data (period + clock → estimated minutes remaining), or a simple heuristic (cancel all orders after 10 PM ET for that day's games).

### Quote skewing

When inventory accumulates, skew quotes to attract the offsetting flow:

- **Long 5 contracts**: widen the bid by 1c (less eager to buy more), keep the ask tight (attract sellers)
- **Short 5 contracts**: keep the bid tight (attract buyers), widen the ask by 1c

This is a standard market-maker inventory management technique. The `skew_threshold` config controls when it kicks in.

### Kill switch

The strategy exposes a `shutdown()` method that:
1. Cancels all resting orders (via `cancel_all` per ticker)
2. Stops posting new quotes
3. Optionally flattens all positions via market orders

Triggered by SIGINT/SIGTERM (same as the existing ingester shutdown path) or by a monitoring alert.

## Rollout plan

### Phase 1: Paper trading (no real orders)

Wire the transform and strategy into the live process, but replace `KalshiOrderClient` with a `PaperOrderClient` that logs order intents to silver without touching Kalshi's API.

**What we validate:**
- Transform produces correct `OrderBookUpdate` / `TradeEvent` from live frames
- Strategy generates reasonable quotes (spreads > min, positions bounded)
- Quote frequency is manageable (not thousands/second)
- Silver captures `MMQuoteEvent` / `MMFillEvent` for post-hoc analysis

**Duration:** 1-2 weeks across multiple game days.

**Success criteria:** paper P&L is positive, position limits respected, no quote storms.

### Phase 2: Live trading, minimum size

Replace `PaperOrderClient` with `KalshiOrderClient`. Start with:
- `order_size = 1` (one contract per side = $1 max exposure per fill)
- `max_position = 5` per ticker
- `min_spread_cents = 5` (extra conservative, only wide spreads)
- Monitor via notebook querying silver `MMFillEvent` Parquet

**Duration:** 1-2 weeks. Enough game days to get ~100 round trips.

**Success criteria:** live fill prices match paper expectations, realized P&L positive, no API issues (rate limits, auth failures, stale quotes).

### Phase 3: Scale up

Based on Phase 2 data:
- Lower `min_spread_cents` to 3 (the simulation breakeven point)
- Increase `order_size` to 5-10 contracts
- Increase `max_position` to 20
- Add position skewing
- Add pre-settlement flattening

### Phase 4: Expand series (optional)

If KXNBAPTS is profitable, evaluate KXNBATOTAL (showed positive edge at $0.03+, balanced buy/sell ratio). Same strategy code, different `ticker.startswith()` filter.

## Files to create/modify

| File | Action | Description |
|---|---|---|
| `app/transforms/kalshi_ws.py` | **Create** | Raw frame → typed event, maintains OrderBookState |
| `app/strategy/mm.py` | **Create** | Quoting logic, position management, skewing |
| `app/strategy/__init__.py` | **Create** | Package init |
| `app/clients/kalshi_rest_orders.py` | **Create** | Place/cancel limit orders via REST |
| `app/events.py` | **Modify** | Add `MMQuoteEvent`, `MMFillEvent` |
| `scripts/live/kalshi_ws/__main__.py` | **Modify** | Wire transform + strategy + silver into main loop |
| `app/core/config.py` | **Modify** | Add `MM_*` config vars (from env/defaults) |
| `tests/transforms/test_kalshi_ws.py` | **Create** | Golden-file tests for transform |
| `tests/strategy/test_mm.py` | **Create** | Unit tests for quoting logic |

## Open questions

1. **Fill detection: inference vs API.** The simulation infers fills by matching trades against our posted prices. In production, Kalshi's REST API provides fill notifications. Use the API as source of truth; the inference path is a fallback for logging.

2. **Order rate limits.** Kalshi's API rate limits are not well documented for order endpoints. Phase 1 (paper) will measure quote update frequency to ensure we're not hitting limits. If updates exceed ~10/second, batch quote changes.

3. **Multi-game concurrency.** During a typical NBA night, 3-5 games overlap. Each game has ~20 KXNBAPTS markets. That's 60-100 markets to quote simultaneously. The single-connection WS handles the data; the question is whether the REST order API handles 100+ resting orders without latency issues.

4. **Game-end timing.** Pre-settlement flattening needs to know when games end. Options: (a) NBA CDN live scoreboard polling (existing `nba_cdn` ingester), (b) Kalshi market lifecycle events (`market_lifecycle_v2` channel — not currently subscribed), (c) simple time heuristic (games rarely go past 11 PM ET).

5. **Should the strategy run in-process or as a sidecar?** `data-flow.md` prescribes in-process for simplicity. But a bug in the strategy could crash the ingester, losing bronze data. Mitigation: the strategy's `on_event` is wrapped in a try/except — strategy exceptions log but don't propagate to the ingester loop.
