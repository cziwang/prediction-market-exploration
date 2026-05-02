"""Tests for app.transforms.kalshi_ws."""

from app.events import BookInvalidated, OrderBookUpdate, TradeEvent
from app.transforms.kalshi_ws import KalshiTransform, OrderBookState


def _snapshot_frame(ticker: str, yes_book: list, no_book: list) -> dict:
    return {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": yes_book,
            "no_dollars_fp": no_book,
        },
    }


def _delta_frame(ticker: str, price: str, delta: str, side: str) -> dict:
    return {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": ticker,
            "price_dollars": price,
            "delta_fp": delta,
            "side": side,
        },
    }


def _trade_frame(ticker: str, yes_price: str, count: int, taker_side: str) -> dict:
    return {
        "type": "trade",
        "msg": {
            "market_ticker": ticker,
            "yes_price_dollars": yes_price,
            "count_fp": str(count),
            "taker_side": taker_side,
        },
    }


class TestOrderBookState:
    def test_from_snapshot(self):
        msg = {
            "yes_dollars_fp": [["0.5000", "10000"], ["0.4900", "5000"]],
            "no_dollars_fp": [["0.4800", "8000"], ["0.4700", "3000"]],
        }
        book = OrderBookState.from_snapshot(msg)
        assert book.best_bid == 50  # 0.50 in cents
        assert book.best_ask == 52  # 1.00 - 0.48 = 0.52
        assert book.bid_size_top == 10000
        assert book.ask_size_top == 8000
        assert book.mid == 51

    def test_apply_delta_add(self):
        book = OrderBookState()
        book.yes_book[50] = 10000
        book.no_book[48] = 8000
        # Add size at bid level
        book.apply_delta({"price_dollars": "0.5000", "delta_fp": "5000", "side": "yes"})
        assert book.yes_book[50] == 15000

    def test_apply_delta_remove(self):
        book = OrderBookState()
        book.yes_book[50] = 10000
        book.no_book[48] = 8000
        # Remove all size at bid level
        book.apply_delta({"price_dollars": "0.5000", "delta_fp": "-10000", "side": "yes"})
        assert 50 not in book.yes_book

    def test_empty_book_properties(self):
        book = OrderBookState()
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.mid is None
        assert book.bid_size_top == 0
        assert book.ask_size_top == 0


class TestKalshiTransform:
    def test_snapshot_produces_book_update(self):
        t = KalshiTransform()
        frame = _snapshot_frame(
            "KXNBAPTS-TEST-P25",
            [["0.4000", "5000"]],
            [["0.5800", "3000"]],
        )
        events = t(frame, t_receipt=1000.0, conn_id="conn-1")
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, OrderBookUpdate)
        assert ev.market_ticker == "KXNBAPTS-TEST-P25"
        assert ev.bid_yes == 40
        assert ev.ask_yes == 42  # 100 - 58
        assert ev.bid_size == 5000
        assert ev.ask_size == 3000

    def test_delta_after_snapshot(self):
        t = KalshiTransform()
        # Seed with snapshot
        snap = _snapshot_frame("T1", [["0.5000", "10000"]], [["0.4800", "8000"]])
        t(snap, t_receipt=1.0, conn_id="c1")
        # Apply delta
        delta = _delta_frame("T1", "0.5100", "6000", "yes")
        events = t(delta, t_receipt=2.0, conn_id="c1")
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, OrderBookUpdate)
        assert ev.bid_yes == 51  # new best bid

    def test_delta_without_snapshot_skipped(self):
        t = KalshiTransform()
        delta = _delta_frame("UNKNOWN", "0.5000", "1000", "yes")
        events = t(delta, t_receipt=1.0, conn_id="c1")
        assert events == []

    def test_trade_produces_trade_event(self):
        t = KalshiTransform()
        frame = _trade_frame("KXNBAPTS-TEST-P25", "0.5500", 3, "yes")
        events = t(frame, t_receipt=1.0, conn_id="c1")
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, TradeEvent)
        assert ev.market_ticker == "KXNBAPTS-TEST-P25"
        assert ev.price == 55
        assert ev.size == 3
        assert ev.side == "yes"

    def test_conn_id_change_invalidates_books(self):
        t = KalshiTransform()
        # Seed two tickers
        t(_snapshot_frame("T1", [["0.50", "10000"]], [["0.48", "8000"]]), 1.0, conn_id="c1")
        t(_snapshot_frame("T2", [["0.30", "5000"]], [["0.68", "4000"]]), 2.0, conn_id="c1")
        assert len(t._books) == 2

        # Change connection
        events = t({"type": "ok"}, t_receipt=3.0, conn_id="c2")
        # Should emit BookInvalidated for both tickers
        invalidated = [e for e in events if isinstance(e, BookInvalidated)]
        assert len(invalidated) == 2
        tickers = {e.market_ticker for e in invalidated}
        assert tickers == {"T1", "T2"}
        # Books should be cleared
        assert len(t._books) == 0

    def test_control_frame_returns_empty(self):
        t = KalshiTransform()
        events = t({"type": "ok"}, t_receipt=1.0, conn_id="c1")
        assert events == []

    def test_same_conn_id_no_invalidation(self):
        t = KalshiTransform()
        t(_snapshot_frame("T1", [["0.50", "10000"]], [["0.48", "8000"]]), 1.0, conn_id="c1")
        events = t({"type": "ok"}, t_receipt=2.0, conn_id="c1")
        assert len(t._books) == 1  # not cleared
        assert events == []
