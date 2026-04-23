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

**Connection-aware book invalidation.** The transform tracks the current `conn_id`. When it changes (WS reconnect), all cached `OrderBookState` entries are invalidated. Deltas from connection B cannot be applied to a snapshot from connection A. The strategy is notified of the invalidation so it can cancel stale orders (see M2 fix below).

```python
class KalshiTransform:
    """Raw WS frame → typed Event. Maintains order book state."""
    
    def __init__(self):
        self._books: dict[str, OrderBookState] = {}
        self._conn_id: str | None = None
    
    def __call__(self, frame: dict, t_receipt: float,
                 conn_id: str | None = None) -> list[Event]:
        """Returns a list of events (usually 0 or 1, but reconnect can emit many)."""
        events: list[Event] = []
        
        # --- Connection change: invalidate all books ---
        if conn_id is not None and conn_id != self._conn_id:
            # Emit a BookInvalidated event for every ticker the strategy was tracking
            for ticker in list(self._books):
                events.append(BookInvalidated(t_receipt=t_receipt, market_ticker=ticker))
            self._books.clear()
            self._conn_id = conn_id
        
        msg_type = frame.get("type")
        
        if msg_type == "orderbook_snapshot":
            ticker = frame["msg"]["market_ticker"]
            self._books[ticker] = OrderBookState.from_snapshot(frame["msg"])
            book = self._books[ticker]
            events.append(OrderBookUpdate(
                t_receipt=t_receipt,
                market_ticker=ticker,
                bid_yes=book.best_bid,
                ask_yes=book.best_ask,
                bid_size=book.bid_size_top,
                ask_size=book.ask_size_top,
            ))
        
        elif msg_type == "orderbook_delta":
            ticker = frame["msg"]["market_ticker"]
            book = self._books.get(ticker)
            if book is None:
                return events  # delta without snapshot — skip
            book.apply_delta(frame["msg"])
            events.append(OrderBookUpdate(
                t_receipt=t_receipt,
                market_ticker=ticker,
                bid_yes=book.best_bid,
                ask_yes=book.best_ask,
                bid_size=book.bid_size_top,
                ask_size=book.ask_size_top,
            ))
        
        elif msg_type == "trade":
            msg = frame["msg"]
            events.append(TradeEvent(
                t_receipt=t_receipt,
                market_ticker=msg["market_ticker"],
                side=msg["taker_side"],       # "yes" or "no"
                price=int(msg["yes_price"]),   # cents
                size=int(msg["count"]),
            ))
        
        return events
```

`OrderBookState` is a helper class that holds the current YES and NO books as `dict[int, int]` (price cents → size cents), applies deltas, and exposes `best_bid`, `best_ask`, `spread`, `mid`. Uses integer cents throughout — no floats for prices.

`BookInvalidated` is a new event type emitted on connection change. The strategy responds by cancelling all resting orders for that ticker and marking its book state as unknown until a fresh `OrderBookUpdate` arrives from the new connection's snapshot.

### 2. Strategy: `app/strategy/mm.py`

The core quoting logic. Receives typed events, decides when to post/cancel orders. Must be non-blocking per `data-flow.md` rules — order placement is fire-and-forget via an async queue.

#### Per-ticker order state machine

The single largest source of accidental double-exposure is sending a new order while a previous one is still in-flight. Each ticker's order state on each side (bid/ask) follows a strict state machine:

```
  idle ──────▶ pending ──────▶ resting ──────▶ cancel_pending ──────▶ idle
   │            │    │           │                  │
   │            │    │           ▼                  │
   │            │    └── failed ──▶ idle            │
   │            │                                   │
   │            └── filled (partial) ──▶ resting    │
   │                                    (reduced)   │
   └──────────────────────────────────────────◀─────┘
```

- **`idle`**: no order on this side. Strategy may submit a new one.
- **`pending`**: order submitted to Kalshi, awaiting ACK. **No new orders allowed** for this ticker+side.
- **`resting`**: order confirmed by Kalshi, sitting on the book. May be cancelled or filled.
- **`cancel_pending`**: cancel submitted, awaiting ACK. No new orders until confirmed.

This prevents ghost orders: if two `OrderBookUpdate` events arrive in rapid succession, the second sees `pending` state and skips the order intent.

```python
class OrderSideState:
    """State machine for one side (bid or ask) of one ticker."""
    state: str = "idle"       # idle | pending | resting | cancel_pending
    order_id: str | None = None
    price: int | None = None
    remaining_size: int = 0   # tracks partial fills

class MMStrategy:
    """Passive market maker on KXNBAPTS."""
    
    def __init__(self, order_client, config: MMConfig):
        self._client = order_client
        self._config = config
        self._positions: dict[str, int] = {}             # ticker → net contracts
        self._aggregate_position: int = 0                 # sum of abs(positions)
        self._order_state: dict[str, dict[str, OrderSideState]] = {}
            # ticker → {"bid": OrderSideState, "ask": OrderSideState}
        self._circuit_breaker: bool = False               # True = suppress all orders
    
    def on_event(self, event: Event) -> None:
        if isinstance(event, OrderBookUpdate):
            self._on_book_update(event)
        elif isinstance(event, TradeEvent):
            self._on_trade(event)
        elif isinstance(event, BookInvalidated):
            self._on_book_invalidated(event)
    
    def _on_book_update(self, update: OrderBookUpdate) -> None:
        ticker = update.market_ticker
        if not ticker.startswith("KXNBAPTS-"):
            return
        if self._circuit_breaker:
            return
        
        spread = update.ask_yes - update.bid_yes
        position = self._positions.get(ticker, 0)
        
        # --- Net-of-fee spread check ---
        mid = (update.bid_yes + update.ask_yes) / 2
        maker_fee_cents = int(0.0175 * mid * (100 - mid) / 100)  # integer cents
        net_half_spread = (spread // 2) - maker_fee_cents
        
        # --- Quoting decision ---
        should_bid = (
            net_half_spread >= self._config.min_edge_cents
            and position < self._config.max_position
            and self._aggregate_position < self._config.max_aggregate_position
        )
        should_ask = (
            net_half_spread >= self._config.min_edge_cents
            and position > -self._config.max_position
            and self._aggregate_position < self._config.max_aggregate_position
        )
        
        # Skew quotes when carrying inventory
        bid_price = update.bid_yes
        ask_price = update.ask_yes
        if position > self._config.skew_threshold:
            bid_price = max(1, bid_price - 1)
        elif position < -self._config.skew_threshold:
            ask_price = min(99, ask_price + 1)
        
        # Only submit if state machine allows it
        self._maybe_update_side(ticker, "bid", bid_price if should_bid else None)
        self._maybe_update_side(ticker, "ask", ask_price if should_ask else None)
    
    def _maybe_update_side(self, ticker: str, side: str, price: int | None) -> None:
        """Submit order intent only if the state machine is in idle or resting."""
        state = self._get_side_state(ticker, side)
        
        if price is None:
            # Want to stop quoting this side
            if state.state == "resting":
                state.state = "cancel_pending"
                self._enqueue_cancel(ticker, side, state.order_id)
            return
        
        if state.state == "idle":
            state.state = "pending"
            state.price = price
            self._enqueue_place(ticker, side, price)
        elif state.state == "resting" and state.price != price:
            # Price changed — cancel old, will re-place on ACK
            state.state = "cancel_pending"
            self._enqueue_cancel(ticker, side, state.order_id)
        # If pending or cancel_pending: do nothing, wait for ACK
    
    def _on_book_invalidated(self, event: BookInvalidated) -> None:
        """Connection changed — cancel all orders for this ticker."""
        ticker = event.market_ticker
        for side in ["bid", "ask"]:
            state = self._get_side_state(ticker, side)
            if state.state in ("resting", "pending"):
                state.state = "cancel_pending"
                self._enqueue_cancel(ticker, side, state.order_id)
    
    def on_order_ack(self, ticker: str, side: str, order_id: str) -> None:
        """Called by the order queue drainer when Kalshi confirms a placement."""
        state = self._get_side_state(ticker, side)
        state.state = "resting"
        state.order_id = order_id
    
    def on_order_rejected(self, ticker: str, side: str, error: str) -> None:
        """Called when Kalshi rejects an order (market closed, etc.)."""
        state = self._get_side_state(ticker, side)
        state.state = "idle"
        state.order_id = None
    
    def on_cancel_ack(self, ticker: str, side: str) -> None:
        """Called when a cancel is confirmed."""
        state = self._get_side_state(ticker, side)
        state.state = "idle"
        state.order_id = None
    
    def on_fill(self, ticker: str, side: str, fill_size: int,
                remaining_size: int) -> None:
        """Called on fill notification (supports partial fills)."""
        state = self._get_side_state(ticker, side)
        
        # Update position
        delta = fill_size if side == "bid" else -fill_size
        old_pos = self._positions.get(ticker, 0)
        self._positions[ticker] = old_pos + delta
        self._aggregate_position = sum(abs(v) for v in self._positions.values())
        
        # Update order state
        state.remaining_size = remaining_size
        if remaining_size == 0:
            state.state = "idle"
            state.order_id = None
```

#### Configuration: `MMConfig`

```python
@dataclass
class MMConfig:
    min_edge_cents: int = 1         # min net edge (half_spread - fee) to quote
    min_spread_cents: int = 3       # hard floor on raw spread (kept for simplicity)
    max_position: int = 10          # per-ticker position limit (contracts)
    max_aggregate_position: int = 200  # across all tickers
    skew_threshold: int = 3         # start skewing quotes at this position
    flatten_before_settle_s: int = 300  # cancel all 5 min before settlement
    order_size: int = 1             # contracts per side
    reconcile_interval_s: int = 30  # how often to reconcile with Kalshi API
    circuit_breaker_threshold: int = 3  # consecutive REST failures before halt
```

Start conservative: 1 contract per side, 10-contract per-ticker limit, 200-contract aggregate limit. Increase sizing after live validation.

#### Position reconciliation loop

The strategy's in-memory positions can drift from Kalshi's actual state (missed fill notification, REST timeout, partial fill edge case). A background task reconciles every `reconcile_interval_s`:

```python
async def _reconciliation_loop(self):
    """Periodic truth-check against Kalshi's REST API."""
    while not self._shutdown.is_set():
        await asyncio.sleep(self._config.reconcile_interval_s)
        try:
            api_positions = await self._client.get_positions()
            api_orders = await self._client.get_open_orders()
        except Exception as e:
            log.warning("reconciliation failed: %s", e)
            continue
        
        # Diff positions
        for ticker in set(list(self._positions) + list(api_positions)):
            internal = self._positions.get(ticker, 0)
            actual = api_positions.get(ticker, 0)
            if internal != actual:
                log.error("POSITION MISMATCH %s: internal=%d actual=%d",
                          ticker, internal, actual)
                self._positions[ticker] = actual  # trust Kalshi
                # Emit reconciliation event for the audit trail
                await self._emit(MMReconcileEvent(...))
        
        # Diff open orders — detect orphaned orders from a prior crash
        for order in api_orders:
            state = self._get_side_state(order.ticker, order.side)
            if state.order_id != order.order_id:
                log.warning("ORPHAN ORDER %s on %s — cancelling",
                            order.order_id, order.ticker)
                await self._client.cancel(order.order_id)
        
        self._aggregate_position = sum(abs(v) for v in self._positions.values())
```

#### Startup recovery

On process start, before processing any WS events:

```python
async def bootstrap(self):
    """Seed internal state from Kalshi's REST API (source of truth)."""
    # 1. Fetch actual positions and open orders from Kalshi
    self._positions = await self._client.get_positions()
    api_orders = await self._client.get_open_orders()
    
    # 2. Cancel any orphaned orders from a prior crash
    #    (We don't know what book state they were based on)
    for order in api_orders:
        log.info("cancelling orphan order %s from prior session", order.order_id)
        await self._client.cancel(order.order_id)
    
    # 3. All order states start as idle (we just cancelled everything)
    #    Book state starts empty (will be populated by incoming snapshots)
    
    self._aggregate_position = sum(abs(v) for v in self._positions.values())
    log.info("bootstrap complete: %d positions, %d orphan orders cancelled",
             len(self._positions), len(api_orders))
```

This is simpler than trying to resume mid-session: cancel everything, start clean, let the incoming snapshots re-establish book state. The cost is a few seconds of not quoting after restart.

### 3. Order client: `app/clients/kalshi_rest_orders.py`

Thin wrapper around Kalshi's REST API for order management. The existing `kalshi_sdk.py` uses the official SDK for market queries; this adds order placement.

```python
class KalshiOrderClient:
    """Place and cancel limit orders on Kalshi via REST.
    
    Includes circuit breaker, auth retry, and callback-based ACK delivery.
    """
    
    async def place_limit(self, ticker: str, side: str, price_cents: int,
                          size: int) -> str:
        """Returns order_id. Raises on failure after retry."""
        ...
    
    async def cancel(self, order_id: str) -> None: ...
    
    async def cancel_all(self, ticker: str) -> None:
        """Cancel all resting orders on a ticker."""
        ...
    
    async def get_positions(self) -> dict[str, int]:
        """Fetch current positions from Kalshi (source of truth)."""
        ...
    
    async def get_open_orders(self) -> list[OpenOrder]:
        """Fetch all resting orders. Used for reconciliation and crash recovery."""
        ...
```

#### Circuit breaker

The order client tracks consecutive REST failures. After `circuit_breaker_threshold` (default 3) consecutive failures, it sets a flag that the strategy checks in `on_event()` to suppress all new order intents. The circuit breaker resets on any successful API call.

```python
class KalshiOrderClient:
    def __init__(self, ...):
        self._consecutive_failures: int = 0
        self._circuit_open: bool = False
    
    async def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = await self._session.request(method, path, **kwargs)
            if resp.status == 401:
                # Auth expired — re-sign and retry once
                self._refresh_auth()
                resp = await self._session.request(method, path, **kwargs)
            resp.raise_for_status()
            self._consecutive_failures = 0
            self._circuit_open = False
            return await resp.json()
        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._config.circuit_breaker_threshold:
                self._circuit_open = True
                log.error("CIRCUIT BREAKER OPEN after %d failures: %s",
                          self._consecutive_failures, e)
            raise
```

When the circuit breaker opens:
1. Strategy stops sending new order intents (checks `_client.circuit_open`)
2. All per-ticker order states are marked as unknown
3. An `MMCircuitBreakerEvent` is emitted to silver for the audit trail
4. The reconciliation loop keeps running — when it succeeds, the breaker resets

#### Queue drainer with ACK callbacks

The order queue sits between the synchronous `on_event()` and the async REST API. The drainer processes intents one at a time and calls back into the strategy to advance the state machine:

```python
async def _order_queue_drainer(self):
    """Background task: drain order intents, call REST, deliver ACKs."""
    while True:
        intent = await self._order_queue.get()
        try:
            if intent.action == "place":
                order_id = await self._client.place_limit(
                    intent.ticker, intent.side, intent.price, intent.size)
                self._strategy.on_order_ack(intent.ticker, intent.side, order_id)
            elif intent.action == "cancel":
                await self._client.cancel(intent.order_id)
                self._strategy.on_cancel_ack(intent.ticker, intent.side)
        except Exception as e:
            self._strategy.on_order_rejected(intent.ticker, intent.side, str(e))
        finally:
            # Emit MMOrderEvent regardless of success/failure
            await self._silver.emit(MMOrderEvent(...))
```

Key design choice: **order placement is async and fire-and-forget from the strategy's perspective.** The strategy pushes order intents onto an `asyncio.Queue`; the drainer calls the REST API and delivers ACKs back to the strategy's state machine. This keeps `on_event()` non-blocking per `data-flow.md` rules while ensuring the state machine transitions are driven by real API responses.

### 4. Event extensions: `app/events.py`

New event types for the transform and strategy layers. All flow through SilverWriter → Parquet for post-hoc analysis and replay.

```python
# --- Transform-level ---

@dataclass(frozen=True)
class BookInvalidated:
    """Emitted when a WS reconnect invalidates a ticker's book state."""
    t_receipt: float
    market_ticker: str

# --- Strategy-level ---

@dataclass(frozen=True)
class MMQuoteEvent:
    """Snapshot of the strategy's quoting decision. Only emitted on changes."""
    t_receipt: float
    market_ticker: str
    bid_price: int | None     # what we're posting (None = not quoting this side)
    ask_price: int | None
    book_bid: int             # market's best bid/ask
    book_ask: int
    spread: int
    position: int
    reason_no_bid: str | None # "spread_narrow" | "pos_limit" | "agg_limit" |
                              # "circuit_breaker" | "book_invalid" | None
    reason_no_ask: str | None

@dataclass(frozen=True)
class MMOrderEvent:
    """Every order placed or cancelled via the Kalshi REST API."""
    t_receipt: float
    t_ack: float | None       # when Kalshi acknowledged (None if failed)
    market_ticker: str
    action: str               # "place_bid" | "place_ask" | "cancel" | "cancel_all"
    price: int | None
    size: int | None
    order_id: str | None
    reason: str               # "spread_wide" | "quote_update" | "flatten" | "shutdown" | ...
    error: str | None         # None on success

@dataclass(frozen=True)
class MMFillEvent:
    """A confirmed fill (supports partial fills)."""
    t_receipt: float
    market_ticker: str
    side: str                 # "buy" or "sell"
    price: int                # cents
    fill_size: int            # how many contracts filled in this event
    order_remaining_size: int # contracts still resting after this fill
    position_before: int
    position_after: int
    maker_fee: int            # cents
    order_id: str
    book_mid_at_fill: int     # for adverse selection measurement

@dataclass(frozen=True)
class MMReconcileEvent:
    """Emitted when the reconciliation loop detects a state mismatch."""
    t_receipt: float
    market_ticker: str
    field: str                # "position" or "order"
    internal_value: str       # what the strategy thought
    actual_value: str         # what Kalshi reported
    action_taken: str         # "corrected" | "cancelled_orphan"

@dataclass(frozen=True)
class MMCircuitBreakerEvent:
    """Emitted when the REST circuit breaker opens or closes."""
    t_receipt: float
    state: str                # "open" | "closed"
    consecutive_failures: int
    last_error: str | None
```

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

## Replay and post-mortem analysis

When something goes wrong — a bad fill, unexpected inventory, a P&L spike — we need to
reconstruct exactly what the strategy saw and did, event by event. The replay system makes
this possible without touching the live process.

### What gets recorded (the three tapes)

Every piece of information needed to replay a session is written to S3 during live trading:

```
1. BRONZE (raw WS frames — already exists)
   bronze/kalshi_ws/orderbook_snapshot/...
   bronze/kalshi_ws/orderbook_delta/...
   bronze/kalshi_ws/trade/...
   → What Kalshi sent us, byte-for-byte. Authoritative. Never modified.

2. SILVER (typed events — transform output)
   silver/kalshi_ws/OrderBookUpdate/date=.../v=N/...
   silver/kalshi_ws/TradeEvent/date=.../v=N/...
   → What the transform produced from bronze. Rebuildable.

3. STRATEGY LOG (decisions + actions — NEW)
   silver/kalshi_ws/MMQuoteEvent/date=.../v=N/...
   silver/kalshi_ws/MMFillEvent/date=.../v=N/...
   silver/kalshi_ws/MMOrderEvent/date=.../v=N/...
   → What the strategy decided and what Kalshi confirmed. The audit trail.
```

Together, these three tapes let you answer: "at time T, what was the book state, what did
the strategy decide, and what actually executed?"

### Strategy log events

Extend `app/events.py` with three strategy-level events (beyond the two already proposed):

```python
@dataclass(frozen=True)
class MMOrderEvent:
    """Every order placed or cancelled via the Kalshi REST API."""
    t_receipt: float          # when we decided to place/cancel
    t_ack: float | None       # when Kalshi acknowledged (None if failed/pending)
    market_ticker: str
    action: str               # "place_bid" | "place_ask" | "cancel" | "cancel_all"
    price: int | None         # cents (None for cancel_all)
    size: int | None
    order_id: str | None      # Kalshi order ID (None if placement failed)
    reason: str               # why: "spread_wide" | "position_limit" | "skew" |
                              #      "flatten" | "shutdown" | "quote_update"
    error: str | None         # None on success, error message on failure

@dataclass(frozen=True)
class MMQuoteEvent:
    """Snapshot of the strategy's quoting decision on each book update."""
    t_receipt: float
    market_ticker: str
    bid_price: int | None     # what we're posting (None = not quoting this side)
    ask_price: int | None
    book_bid: int             # what the market's best bid/ask is
    book_ask: int
    spread: int
    position: int             # our net position on this ticker
    reason_no_bid: str | None # why we're not bidding: "spread_narrow" | "pos_limit" | None
    reason_no_ask: str | None

@dataclass(frozen=True)
class MMFillEvent:
    """A confirmed fill on one of our resting orders."""
    t_receipt: float
    market_ticker: str
    side: str                 # "buy" or "sell"
    price: int                # cents
    size: int
    position_before: int
    position_after: int
    maker_fee: int            # cents
    order_id: str
    book_mid_at_fill: int     # for adverse selection measurement
```

Key design choices:
- **`MMOrderEvent` logs every API call** including failures and the reason. This is the
  most important tape for debugging: "why did we place that order? why did it fail?"
- **`MMQuoteEvent` logs the decision, not just the action.** When we skip quoting, the
  `reason_no_bid` / `reason_no_ask` field says why. This is critical for debugging
  "why didn't we quote when the spread was wide?" scenarios.
- **`MMFillEvent` captures `book_mid_at_fill`** so we can compute adverse selection on
  live fills without reconstructing the book.

### The replay tool: `scripts/replay/mm.py`

A standalone script that replays a historical session through the strategy, offline.
It reads bronze (or silver), feeds events through the same `KalshiTransform` +
`MMStrategy` code, but with a `ReplayOrderClient` instead of the real Kalshi API.

```python
# scripts/replay/mm.py
"""Replay a historical session through the MM strategy.

Usage:
    python -m scripts.replay.mm --date 2026-04-20 --ticker "KXNBAPTS-*"
    python -m scripts.replay.mm --date 2026-04-20 --ticker "KXNBAPTS-26APR20ATLNYK-TREYOU25"
    python -m scripts.replay.mm --from-bronze --date 2026-04-20   # replay from raw frames
"""

class ReplayOrderClient:
    """Drop-in for KalshiOrderClient. Simulates order lifecycle synchronously.
    
    Key design: mirrors the live async path by immediately delivering ACKs
    back to the strategy's state machine (on_order_ack, on_fill, etc.).
    This closes the structural divergence between live (async ACKs affect
    position → affect quoting) and replay (must produce the same sequence
    of state transitions).
    
    Fill simulation: when a resting order exists and a TradeEvent arrives
    at the same price on the opposite side, the ReplayOrderClient calls
    strategy.on_fill() to advance the state machine — same as the live
    path would when Kalshi notifies us of a fill.
    """
    
    def __init__(self, strategy: MMStrategy):
        self._strategy = strategy
        self._resting: dict[str, dict] = {}  # order_id → {ticker, side, price, size}
        self.order_log: list[dict] = []
        self._next_id: int = 0
    
    async def place_limit(self, ticker, side, price_cents, size):
        order_id = f"replay-{self._next_id}"
        self._next_id += 1
        self._resting[order_id] = {
            "ticker": ticker, "side": side,
            "price": price_cents, "remaining": size,
        }
        self.order_log.append({"action": "place", "order_id": order_id,
                               "ticker": ticker, "side": side, "price": price_cents})
        # Deliver ACK synchronously — mirrors the live queue drainer callback
        self._strategy.on_order_ack(ticker, side, order_id)
        return order_id
    
    async def cancel(self, order_id):
        info = self._resting.pop(order_id, None)
        if info:
            self._strategy.on_cancel_ack(info["ticker"], info["side"])
    
    def check_fill(self, trade: TradeEvent):
        """Called on every TradeEvent to simulate fills on resting orders."""
        for oid, info in list(self._resting.items()):
            if info["ticker"] != trade.market_ticker:
                continue
            # Buy order filled when a sell (taker_side=="no") hits our bid price
            # Sell order filled when a buy (taker_side=="yes") hits our ask price
            if (info["side"] == "bid" and trade.side == "no"
                    and trade.price == info["price"]):
                fill_size = min(info["remaining"], trade.size)
                info["remaining"] -= fill_size
                self._strategy.on_fill(info["ticker"], "bid", fill_size, info["remaining"])
                if info["remaining"] == 0:
                    del self._resting[oid]
                return
            if (info["side"] == "ask" and trade.side == "yes"
                    and trade.price == info["price"]):
                fill_size = min(info["remaining"], trade.size)
                info["remaining"] -= fill_size
                self._strategy.on_fill(info["ticker"], "ask", fill_size, info["remaining"])
                if info["remaining"] == 0:
                    del self._resting[oid]
                return

async def replay(date: str, ticker_filter: str, from_bronze: bool = False):
    config = MMConfig(...)  # same config as live, or override for experiments
    strategy = MMStrategy(client=None, config=config)  # client set below
    client = ReplayOrderClient(strategy)
    strategy._client = client
    transform = KalshiTransform()
    
    if from_bronze:
        events = replay_from_bronze(date, ticker_filter)
    else:
        events = replay_from_silver(date, ticker_filter)
    
    for event in events:
        strategy.on_event(event)
        # After each TradeEvent, check if it would fill one of our resting orders
        if isinstance(event, TradeEvent):
            client.check_fill(event)
    
    return ReplayReport(client.order_log, strategy._positions)
```

### Replay modes

| Mode | Source | Speed | When to use |
|---|---|---|---|
| **Silver replay** | `silver/.../OrderBookUpdate/` + `TradeEvent/` | Fast (~seconds) | Quick P&L check, parameter sweep, "what would have happened with different config?" |
| **Bronze replay** | `bronze/kalshi_ws/orderbook_delta/` + `trade/` | Slower (re-runs transform) | When you suspect a transform bug, or after updating the transform and want to regenerate results |
| **Live-vs-replay diff** | Silver events + strategy log `MMQuoteEvent` / `MMOrderEvent` | Fast | Compare what the strategy actually did live vs what replay says it should have done. Any divergence = bug. |

### The diff: live vs replay

The most powerful debugging tool. After a session:

```python
# scripts/replay/diff.py
"""Compare live strategy log against offline replay."""

def diff_session(date: str):
    # 1. Load live MMQuoteEvents from silver
    live_quotes = load_silver("MMQuoteEvent", date)
    
    # 2. Replay the same events through the strategy
    replay_quotes = replay(date, ticker_filter="KXNBAPTS-*")
    
    # 3. Compare, event by event
    for live, replayed in align_by_timestamp(live_quotes, replay_quotes):
        if live.bid_price != replayed.bid_price or live.ask_price != replayed.ask_price:
            print(f"DIVERGENCE at {live.t_receipt}: "
                  f"live bid={live.bid_price} ask={live.ask_price}, "
                  f"replay bid={replayed.bid_price} ask={replayed.ask_price}")
```

If the live strategy and the replay strategy produce different quotes from the same
events, something is non-deterministic — a bug. This catches:
- State leaking between tickers
- Time-dependent behavior (using wall clock instead of `t_receipt`)
- Race conditions in the async order queue

### Post-mortem notebook: `notebooks/strategies/mm_postmortem.ipynb`

A template notebook for investigating specific sessions. Loads all three tapes for a
given date and produces:

1. **Timeline view**: book state + strategy quotes + fills on a single time axis
2. **Why-did-we-trade table**: each fill with the `MMQuoteEvent` that triggered it,
   the `MMOrderEvent` that placed the order, the book state at fill time, and the
   adverse selection at 1/5/10/30/60s
3. **Position waterfall**: how inventory accumulated, which fills contributed, which
   games drove the imbalance
4. **P&L attribution**: per-ticker, per-game, per-hour breakdown
5. **What-if analysis**: re-run replay with different config (e.g., tighter position
   limits) and compare P&L

### Strategy determinism requirement

For replay to be trustworthy, the strategy must be **deterministic given the same event
stream**. This means:

- **No wall-clock calls.** Use `event.t_receipt` for all timing decisions (e.g.,
  "has the spread been wide for 5 seconds?"), never `time.time()`.
- **No random number generation** unless seeded deterministically from event data.
- **No external state queries** during `on_event()`. The strategy's state must be
  fully reconstructable from the event stream. Position reconciliation with Kalshi's
  API happens on a separate cadence (e.g., every 60 seconds), not inside `on_event()`.

This is enforced by convention and by the live-vs-replay diff: any non-determinism
shows up as a divergence.

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
| `app/transforms/kalshi_ws.py` | **Create** | Raw frame → typed event, conn_id-aware book invalidation |
| `app/strategy/mm.py` | **Create** | Order state machine, reconciliation loop, quoting logic |
| `app/strategy/__init__.py` | **Create** | Package init |
| `app/clients/kalshi_rest_orders.py` | **Create** | Place/cancel with circuit breaker, auth retry, ACK callbacks |
| `app/events.py` | **Modify** | Add `BookInvalidated`, `MMQuoteEvent`, `MMFillEvent`, `MMOrderEvent`, `MMReconcileEvent`, `MMCircuitBreakerEvent` |
| `scripts/live/kalshi_ws/__main__.py` | **Modify** | Wire transform + strategy + silver into main loop; pass `conn_id` to transform |
| `app/core/config.py` | **Modify** | Add `MMConfig` fields (from env/defaults) |
| `scripts/replay/mm.py` | **Create** | Offline replay with fill-simulating `ReplayOrderClient` |
| `scripts/replay/diff.py` | **Create** | Live-vs-replay divergence detector |
| `notebooks/strategies/mm_postmortem.ipynb` | **Create** | Template notebook for session post-mortems |
| `tests/transforms/test_kalshi_ws.py` | **Create** | Golden-file tests for transform incl. conn_id transitions |
| `tests/strategy/test_mm.py` | **Create** | Order state machine transitions, reconciliation, partial fills |
| `tests/strategy/test_state_machine.py` | **Create** | Exhaustive state machine transition tests |
| `tests/replay/test_determinism.py` | **Create** | Replay same session twice, assert identical output |

## Resolved issues (from quant dev review)

Issues identified by engineering review and addressed in this version:

| ID | Issue | Severity | Resolution |
|---|---|---|---|
| C1 | No order state reconciliation | Critical | 30s reconciliation loop against `get_positions()` + `get_open_orders()` |
| C2 | Ghost orders from fire-and-forget queue | Critical | Per-ticker order state machine (`idle→pending→resting→cancel_pending`) |
| C3 | No crash recovery for in-flight orders | Critical | `bootstrap()` calls REST API on startup, cancels orphans |
| C4 | Partial fills not modeled | Critical | `OrderSideState.remaining_size`, `MMFillEvent.fill_size` / `order_remaining_size` |
| M1 | Replay structural divergence | Major | `ReplayOrderClient` simulates fills by matching resting orders against trade stream |
| M2 | Book state lost on reconnect | Major | Transform tracks `conn_id`, emits `BookInvalidated`, strategy cancels stale orders |
| M3 | REST down while WS up | Major | Circuit breaker after N consecutive REST failures, suppresses order intents |
| M4 | Auth token expiry | Major | Re-sign and retry once on 401 in `_call()` |
| M5 | No aggregate position limit | Major | `max_aggregate_position` in `MMConfig`, checked in `_on_book_update()` |

## Open questions

1. **Order rate limits.** Kalshi's API rate limits are not well documented for order endpoints. Phase 1 (paper) will measure quote update frequency to ensure we're not hitting limits. If updates exceed ~10/second, batch quote changes or use Kalshi's batch order API if available.

2. **Multi-game concurrency.** During a typical NBA night, 3-5 games overlap. Each game has ~20 KXNBAPTS markets. That's 60-100 markets to quote simultaneously. The single-connection WS handles the data; the question is whether the REST order API handles 100+ resting orders without latency issues.

3. **Game-end timing.** Pre-settlement flattening needs to know when games end. Options: (a) subscribe to Kalshi's `market_lifecycle_v2` channel (cleanest — market close events), (b) NBA CDN scoreboard polling, (c) time heuristic (cancel all after 11 PM ET). Recommend (a) as Phase 3 work.

4. **Strategy isolation.** The strategy runs in-process per `data-flow.md`. A strategy bug could crash the ingester, losing bronze data. Mitigation: `on_event()` wrapped in try/except, strategy exceptions log but don't propagate. The order queue drainer is similarly protected. If this proves insufficient, extract to a sidecar process communicating via unix socket.

5. **Strategy log volume.** `MMQuoteEvent` only emitted when the quoting decision *changes* (new price, start/stop quoting), not on every book update. Rate-limited to 1 per ticker per second for unchanged states. `SilverWriter` buffer gets a high-water mark that triggers early flush if rows exceed 100k.

6. **Fill notification source.** Kalshi REST API provides fill notifications (polling `get_fills()`). Use this as source of truth for `on_fill()` callbacks, not trade-stream inference. The reconciliation loop already polls positions; extend it to poll recent fills.
