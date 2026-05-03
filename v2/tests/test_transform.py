"""Tests for KalshiTransform — trade-fill book updates."""

from v2.app.transforms.kalshi_ws import KalshiTransform


def _snapshot_frame(ticker: str, yes_levels: list, no_levels: list,
                    sid: int = 1, seq: int = 1) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "market_id": "test-id",
            "yes_dollars_fp": yes_levels,
            "no_dollars_fp": no_levels,
        },
    }


def _trade_frame(ticker: str, yes_price: str, count: str, taker_side: str,
                 sid: int = 1, seq: int = 10) -> dict:
    return {
        "type": "trade",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "market_id": "test-id",
            "yes_price_dollars": yes_price,
            "count_fp": count,
            "taker_side": taker_side,
            "ts": "1714500000000",
        },
    }


def _delta_frame(ticker: str, price: str, delta: str, side: str,
                 sid: int = 1, seq: int = 5) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "market_id": "test-id",
            "price_dollars": price,
            "delta_fp": delta,
            "side": side,
            "ts": "1714500000000",
        },
    }


TICKER = "KXNBA-TEST"
T = 1714500000.0


class TestTradeFillUpdatesBook:
    """Kalshi's orderbook_delta channel does not emit deltas for matched
    fills.  The transform must decrement the consumed book side when it
    sees a trade so the reconstructed book stays in sync with reality."""

    def _seeded_transform(self):
        """Return a transform with a book: YES 55c x1000, NO 44c x2000."""
        tf = KalshiTransform()
        tf(_snapshot_frame(
            TICKER,
            yes_levels=[["0.5500", "1000.00"]],
            no_levels=[["0.4400", "2000.00"]],
        ), T, conn_id="c1")
        return tf

    def test_yes_taker_decrements_no_book(self):
        tf = self._seeded_transform()
        # YES taker buys at 56c → consumes NO resting at 44c (100-56=44)
        result = tf(_trade_frame(TICKER, "0.5600", "500.00", "yes", seq=10), T + 1, conn_id="c1")
        book = tf._books[TICKER]
        assert book.no_book[44] == 1500  # 2000 - 500

        # Should emit a depth row reflecting the updated book
        assert len(result.depth_rows) == 1
        assert result.depth_rows[0]["ask_1_size"] == 1500

    def test_no_taker_decrements_yes_book(self):
        tf = self._seeded_transform()
        # NO taker at YES price 55c → consumes resting YES at 55c
        result = tf(_trade_frame(TICKER, "0.5500", "300.00", "no", seq=10), T + 1, conn_id="c1")
        book = tf._books[TICKER]
        assert book.yes_book[55] == 700  # 1000 - 300

        assert len(result.depth_rows) == 1
        assert result.depth_rows[0]["bid_1_size"] == 700

    def test_fill_clears_level_entirely(self):
        tf = self._seeded_transform()
        # Fill the entire NO level
        tf(_trade_frame(TICKER, "0.5600", "2000.00", "yes", seq=10), T + 1, conn_id="c1")
        book = tf._books[TICKER]
        assert 44 not in book.no_book
        assert book.best_ask is None

    def test_fill_beyond_resting_clears_level(self):
        tf = self._seeded_transform()
        # Fill more than what's resting (shouldn't happen in practice, but be safe)
        tf(_trade_frame(TICKER, "0.5600", "3000.00", "yes", seq=10), T + 1, conn_id="c1")
        book = tf._books[TICKER]
        assert 44 not in book.no_book

    def test_no_book_means_no_depth_row(self):
        tf = KalshiTransform()
        # Trade for a ticker we haven't seen a snapshot for
        result = tf(_trade_frame(TICKER, "0.5600", "100.00", "yes", seq=10), T, conn_id="c1")
        # Should still produce the TradeEvent
        assert len(result.events) == 1
        # But no depth row (no book to update)
        assert len(result.depth_rows) == 0

    def test_trade_still_produces_event(self):
        tf = self._seeded_transform()
        result = tf(_trade_frame(TICKER, "0.5600", "500.00", "yes", seq=10), T + 1, conn_id="c1")
        assert len(result.events) == 1
        assert result.events[0].price == 56
        assert result.events[0].size == 500
        assert result.events[0].side == "yes"

    def test_sequence_snapshot_delta_trade(self):
        """Full sequence: snapshot → delta → trade. Book should reflect all three."""
        tf = KalshiTransform()

        # Snapshot: YES 50c x100, NO 40c x200
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
        ), T, conn_id="c1")

        # Delta: add YES 55c x500
        tf(_delta_frame(TICKER, "0.5500", "500.00", "yes", seq=5), T + 1, conn_id="c1")

        # Trade: NO taker at YES price 55c, size 200
        # → should consume 200 from YES at 55c
        result = tf(_trade_frame(TICKER, "0.5500", "200.00", "no", seq=10), T + 2, conn_id="c1")

        book = tf._books[TICKER]
        assert book.yes_book[55] == 300  # 500 - 200
        assert book.yes_book[50] == 100  # untouched
        assert book.no_book[40] == 200   # untouched
