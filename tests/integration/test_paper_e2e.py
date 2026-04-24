"""End-to-end test: feed realistic frames through transform → strategy.

Uses synthetic fixture frames that mimic real Kalshi WS data for KXNBAPTS.
Verifies the full pipeline produces orders, respects limits, and has no stuck states.
"""

from app.events import MMFillEvent, MMQuoteEvent, OrderBookUpdate, TradeEvent
from app.strategy.mm import MMConfig, MMStrategy, PaperOrderClient
from app.transforms.kalshi_ws import KalshiTransform

TICKER = "KXNBAPTS-26APR20ATLNYK-TREYOU25"


def _build_fixture_frames():
    """Build a realistic sequence: snapshot → deltas → trades."""
    frames = []

    # 1. Snapshot: best YES bid $0.40, best NO bid $0.55 → YES ask = $0.45, spread = 5c
    #    NO book: bids at $0.55 (= YES ask $0.45) and $0.50 (= YES ask $0.50)
    frames.append(("orderbook_snapshot", {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": TICKER,
            "yes_dollars_fp": [["0.4000", "5000"], ["0.3500", "3000"]],
            "no_dollars_fp": [["0.5500", "4000"], ["0.5000", "2000"]],
        },
    }))

    # 2. A few deltas shifting the book — new YES bid at $0.41
    frames.append(("orderbook_delta", {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": TICKER,
            "price_dollars": "0.4100",
            "delta_fp": "6000",
            "side": "yes",
        },
    }))

    # 3. Trade hitting our bid (taker sells YES = taker_side "no") at $0.41
    frames.append(("trade", {
        "type": "trade",
        "msg": {
            "market_ticker": TICKER,
            "yes_price": "0.4100",
            "count": 1,
            "taker_side": "no",
        },
    }))

    # 4. Trade hitting our ask (taker buys YES = taker_side "yes") at $0.45
    frames.append(("trade", {
        "type": "trade",
        "msg": {
            "market_ticker": TICKER,
            "yes_price": "0.4500",
            "count": 1,
            "taker_side": "yes",
        },
    }))

    # 5. More deltas + trades
    frames.append(("orderbook_delta", {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": TICKER,
            "price_dollars": "0.4100",
            "delta_fp": "-3000",
            "side": "yes",
        },
    }))

    # 6. Another game's snapshot (should be ignored if not KXNBAPTS)
    frames.append(("orderbook_snapshot", {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXNBAGAME-26APR20ATLNYK-ATL",
            "yes_dollars_fp": [["0.5000", "20000"]],
            "no_dollars_fp": [["0.4900", "18000"]],
        },
    }))

    return frames


def test_e2e_paper_trading():
    """Full pipeline: fixture frames → transform → strategy → verify."""
    transform = KalshiTransform()
    config = MMConfig(min_spread_cents=3, max_position=5, order_size=1)
    strategy = MMStrategy(order_client=None, config=config)  # type: ignore[arg-type]
    client = PaperOrderClient(strategy)
    strategy._client = client

    frames = _build_fixture_frames()
    conn_id = "test-conn-1"
    all_events = []

    for i, (_, frame) in enumerate(frames):
        t = 1000.0 + i * 0.5
        events = transform(frame, t_receipt=t, conn_id=conn_id)
        for event in events:
            strategy.on_event(event)
            all_events.append(event)

    # --- Assertions ---

    # 1. Orders were placed on the KXNBAPTS ticker
    places = [o for o in client.order_log if o["action"].startswith("place")]
    assert len(places) >= 2, f"Expected at least 2 order placements, got {len(places)}"
    kxnbapts_places = [o for o in places if TICKER in o["ticker"]]
    assert len(kxnbapts_places) >= 2

    # 2. No orders on KXNBAGAME
    game_places = [o for o in places if "KXNBAGAME" in o["ticker"]]
    assert len(game_places) == 0

    # 3. Positions are within limits
    for ticker, pos in strategy._positions.items():
        assert abs(pos) <= config.max_position, \
            f"Position {pos} on {ticker} exceeds limit {config.max_position}"

    # 4. No stuck pending/cancel_pending states at end
    for ticker, sides in strategy._order_state.items():
        for side_name, state in sides.items():
            assert state.state in ("idle", "resting"), \
                f"{ticker} {side_name} stuck in {state.state}"

    # 5. Strategy emitted quote events
    quotes = [e for e in strategy.pending_events if isinstance(e, MMQuoteEvent)]
    assert len(quotes) >= 1

    # 6. Transform produced OrderBookUpdate and TradeEvent
    book_updates = [e for e in all_events if isinstance(e, OrderBookUpdate)]
    trades = [e for e in all_events if isinstance(e, TradeEvent)]
    assert len(book_updates) >= 2
    assert len(trades) >= 2


def test_e2e_connection_change():
    """Verify conn_id change invalidates books and cancels orders."""
    transform = KalshiTransform()
    config = MMConfig(min_spread_cents=3, max_position=5, order_size=1)
    strategy = MMStrategy(order_client=None, config=config)  # type: ignore[arg-type]
    client = PaperOrderClient(strategy)
    strategy._client = client

    # Snapshot on conn-1
    snap = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": TICKER,
            "yes_dollars_fp": [["0.4000", "5000"]],
            "no_dollars_fp": [["0.5500", "4000"]],
        },
    }
    events = transform(snap, t_receipt=1.0, conn_id="conn-1")
    for e in events:
        strategy.on_event(e)

    # Orders should be resting
    assert strategy._get_side(TICKER, "bid").state == "resting"
    assert strategy._get_side(TICKER, "ask").state == "resting"

    # Connection change: new snapshot on conn-2
    events = transform(snap, t_receipt=2.0, conn_id="conn-2")
    for e in events:
        strategy.on_event(e)

    # After invalidation + re-snapshot, orders should be resting again (new ones)
    cancels = [o for o in client.order_log if o["action"] == "cancel"]
    assert len(cancels) >= 2  # old orders cancelled
