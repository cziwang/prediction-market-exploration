"""Tests for L2 order book construction and delta application."""

import pytest

from src.book import (
    BookManager,
    L2Book,
    dollars_to_cents,
    parse_size,
)


# -- Unit helpers --------------------------------------------------------


class TestConversions:
    def test_dollars_to_cents(self):
        assert dollars_to_cents("0.5500") == 55
        assert dollars_to_cents("0.0100") == 1
        assert dollars_to_cents("0.9900") == 99
        assert dollars_to_cents("0.5000") == 50

    def test_parse_size(self):
        assert parse_size("1000.00") == 1000
        assert parse_size("1.00") == 1
        assert parse_size("0.00") == 0


# -- L2Book --------------------------------------------------------------


def _make_snapshot_msg(
    ticker: str = "TEST-MARKET",
    yes_levels: list[tuple[str, str]] | None = None,
    no_levels: list[tuple[str, str]] | None = None,
) -> dict:
    return {
        "market_ticker": ticker,
        "market_id": "id-123",
        "yes_dollars_fp": yes_levels or [],
        "no_dollars_fp": no_levels or [],
    }


class TestL2Book:
    def test_from_snapshot_basic(self):
        msg = _make_snapshot_msg(
            yes_levels=[("0.5500", "100.00"), ("0.5000", "200.00")],
            no_levels=[("0.4400", "150.00")],
        )
        book = L2Book.from_snapshot(msg, sid=1, seq=10)

        assert book.market_ticker == "TEST-MARKET"
        assert book.yes_bids == {55: 100, 50: 200}
        assert book.no_bids == {44: 150}
        assert book.sid == 1
        assert book.seq == 10

    def test_bbo(self):
        msg = _make_snapshot_msg(
            yes_levels=[("0.5500", "100.00"), ("0.5000", "200.00")],
            no_levels=[("0.4400", "150.00"), ("0.4000", "50.00")],
        )
        book = L2Book.from_snapshot(msg, sid=1, seq=1)

        assert book.best_yes_bid == 55
        # best ask = 100 - max(no_bids) = 100 - 44 = 56
        assert book.best_yes_ask == 56
        assert book.spread == 1
        assert book.midpoint == 55.5

    def test_empty_book(self):
        msg = _make_snapshot_msg()
        book = L2Book.from_snapshot(msg, sid=1, seq=1)

        assert book.best_yes_bid is None
        assert book.best_yes_ask is None
        assert book.spread is None
        assert book.midpoint is None
        assert not book.is_crossed

    def test_apply_delta_add(self):
        book = L2Book(market_ticker="T")
        book.apply_delta(55, 100, "yes")
        assert book.yes_bids == {55: 100}

    def test_apply_delta_increase(self):
        book = L2Book(market_ticker="T", yes_bids={55: 100})
        book.apply_delta(55, 50, "yes")
        assert book.yes_bids[55] == 150

    def test_apply_delta_negative_removes_level(self):
        book = L2Book(market_ticker="T", yes_bids={55: 100})
        book.apply_delta(55, -100, "yes")
        assert 55 not in book.yes_bids

    def test_apply_delta_negative_partial(self):
        book = L2Book(market_ticker="T", yes_bids={55: 100})
        book.apply_delta(55, -30, "yes")
        assert book.yes_bids[55] == 70

    def test_apply_delta_no_side(self):
        book = L2Book(market_ticker="T")
        book.apply_delta(44, 200, "no")
        assert book.no_bids == {44: 200}

    def test_crossed_detection(self):
        # YES bid at 55, NO bid at 46 -> YES ask = 54 -> crossed (55 >= 54)
        book = L2Book(
            market_ticker="T",
            yes_bids={55: 100},
            no_bids={46: 100},
        )
        assert book.is_crossed

    def test_not_crossed(self):
        book = L2Book(
            market_ticker="T",
            yes_bids={55: 100},
            no_bids={44: 100},
        )
        assert not book.is_crossed

    def test_depth_levels(self):
        book = L2Book(
            market_ticker="T",
            yes_bids={55: 100, 50: 200, 45: 300},
            no_bids={44: 150, 40: 50, 35: 75},
        )
        bids = book.yes_bid_levels(depth=2)
        assert bids == [(55, 100), (50, 200)]

        asks = book.yes_ask_levels(depth=2)
        # NO bids 44,40,35 -> YES asks 56,60,65
        assert asks == [(56, 150), (60, 50)]

    def test_repr(self):
        book = L2Book(
            market_ticker="MKT",
            yes_bids={55: 100},
            no_bids={44: 150},
        )
        assert "MKT" in repr(book)
        assert "55c" in repr(book)
        assert "56c" in repr(book)


# -- BookManager ---------------------------------------------------------


def _snapshot_frame(
    ticker: str = "TEST",
    yes: list[tuple[str, str]] | None = None,
    no: list[tuple[str, str]] | None = None,
    sid: int = 1,
    seq: int = 1,
) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": _make_snapshot_msg(ticker, yes, no),
    }


def _delta_frame(
    ticker: str = "TEST",
    price: str = "0.5500",
    delta: str = "100.00",
    side: str = "yes",
    sid: int = 1,
    seq: int = 2,
) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "market_id": "id-123",
            "price_dollars": price,
            "delta_fp": delta,
            "side": side,
            "ts": "1714500000000",
        },
    }


class TestBookManager:
    def test_snapshot_creates_book(self):
        mgr = BookManager()
        frame = _snapshot_frame(
            yes=[("0.5500", "100.00")],
            no=[("0.4400", "150.00")],
        )
        book = mgr.process_frame(frame)
        assert book is not None
        assert book.best_yes_bid == 55

    def test_delta_applies_to_existing_book(self):
        mgr = BookManager()
        mgr.process_frame(_snapshot_frame(yes=[("0.5500", "100.00")]))
        book = mgr.process_frame(_delta_frame(price="0.5500", delta="50.00", side="yes"))
        assert book is not None
        assert book.yes_bids[55] == 150

    def test_delta_without_snapshot_returns_none(self):
        mgr = BookManager()
        result = mgr.process_frame(_delta_frame())
        assert result is None

    def test_stale_sid_dropped(self):
        mgr = BookManager()
        mgr.process_frame(_snapshot_frame(sid=2))
        # Delta with old sid=1 should be dropped.
        result = mgr.process_frame(_delta_frame(sid=1))
        assert result is None

    def test_crossed_book_invalidated(self):
        mgr = BookManager()
        # Start with a valid book: YES bid 55, NO bid 44 -> ask 56.
        mgr.process_frame(
            _snapshot_frame(
                yes=[("0.5500", "100.00")],
                no=[("0.4400", "150.00")],
            )
        )
        # Add NO bid at 46 -> YES ask = 54 -> crossed with bid 55.
        result = mgr.process_frame(_delta_frame(price="0.4600", delta="200.00", side="no"))
        # Book should be invalidated.
        assert result is None
        assert mgr.get("TEST") is None

    def test_new_snapshot_replaces_old(self):
        mgr = BookManager()
        mgr.process_frame(_snapshot_frame(yes=[("0.5500", "100.00")], sid=1, seq=1))
        mgr.process_frame(_snapshot_frame(yes=[("0.6000", "200.00")], sid=2, seq=5))
        book = mgr.get("TEST")
        assert book.yes_bids == {60: 200}
        assert book.sid == 2

    def test_trade_frame_ignored(self):
        mgr = BookManager()
        mgr.process_frame(_snapshot_frame(yes=[("0.5500", "100.00")]))
        trade = {
            "type": "trade",
            "sid": 1,
            "seq": 3,
            "msg": {
                "market_ticker": "TEST",
                "yes_price_dollars": "0.5500",
                "count_fp": "10.00",
                "taker_side": "yes",
                "ts": "1714500000000",
            },
        }
        result = mgr.process_frame(trade)
        assert result is None
        # Book unchanged.
        assert mgr.get("TEST").yes_bids[55] == 100

    def test_invalidate_all(self):
        mgr = BookManager()
        mgr.process_frame(_snapshot_frame(ticker="A", yes=[("0.5500", "100.00")]))
        mgr.process_frame(_snapshot_frame(ticker="B", yes=[("0.6000", "200.00")]))
        mgr.invalidate_all()
        assert mgr.get("A") is None
        assert mgr.get("B") is None

    def test_negative_delta_fill(self):
        mgr = BookManager()
        mgr.process_frame(_snapshot_frame(yes=[("0.5500", "100.00")], no=[("0.4400", "200.00")]))
        # Fill removes 30 from YES bid at 55.
        book = mgr.process_frame(_delta_frame(price="0.5500", delta="-30.00", side="yes"))
        assert book is not None
        assert book.yes_bids[55] == 70

    def test_delta_removes_level_entirely(self):
        mgr = BookManager()
        mgr.process_frame(_snapshot_frame(yes=[("0.5500", "100.00")], no=[("0.4400", "200.00")]))
        book = mgr.process_frame(_delta_frame(price="0.5500", delta="-100.00", side="yes"))
        assert book is not None
        assert 55 not in book.yes_bids
