"""Tests for KalshiTransform.

Covers: snapshot/delta/trade handling, fill-delta interaction,
reconnect (conn_id change) with BookInvalidated, multi-ticker
replay scenarios, and real bronze data validation.
"""

import gzip
import json
import os
from collections import Counter

import boto3
import pytest

from v2.app.transforms.kalshi_ws import KalshiTransform
from v2.app.events import BookInvalidated, TradeEvent


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


class TestTradeDoesNotModifyBook:
    """Kalshi's orderbook_delta channel emits negative deltas for matched
    fills.  Trades must NOT modify BookState — the delta handles it."""

    def _seeded_transform(self):
        """Return a transform with a book: YES 55c x1000, NO 44c x2000."""
        tf = KalshiTransform()
        tf(_snapshot_frame(
            TICKER,
            yes_levels=[["0.5500", "1000.00"]],
            no_levels=[["0.4400", "2000.00"]],
        ), T, conn_id="c1")
        return tf

    def test_yes_taker_does_not_change_book(self):
        tf = self._seeded_transform()
        tf(_trade_frame(TICKER, "0.5600", "500.00", "yes", seq=10), T + 1, conn_id="c1")
        book = tf._books[TICKER]
        # NO book should be untouched — delta channel handles the fill
        assert book.no_book[44] == 2000

    def test_no_taker_does_not_change_book(self):
        tf = self._seeded_transform()
        tf(_trade_frame(TICKER, "0.5500", "300.00", "no", seq=10), T + 1, conn_id="c1")
        book = tf._books[TICKER]
        # YES book should be untouched
        assert book.yes_book[55] == 1000

    def test_trade_does_not_produce_depth_row(self):
        tf = self._seeded_transform()
        result = tf(_trade_frame(TICKER, "0.5600", "500.00", "yes", seq=10), T + 1, conn_id="c1")
        assert len(result.depth_rows) == 0

    def test_trade_still_produces_event(self):
        tf = self._seeded_transform()
        result = tf(_trade_frame(TICKER, "0.5600", "500.00", "yes", seq=10), T + 1, conn_id="c1")
        assert len(result.events) == 1
        evt = result.events[0]
        assert isinstance(evt, TradeEvent)
        assert evt.price == 56
        assert evt.size == 500
        assert evt.side == "yes"

    def test_no_book_means_no_depth_row(self):
        tf = KalshiTransform()
        # Trade for a ticker we haven't seen a snapshot for
        result = tf(_trade_frame(TICKER, "0.5600", "100.00", "yes", seq=10), T, conn_id="c1")
        assert len(result.events) == 1
        assert len(result.depth_rows) == 0

    def test_fill_delta_updates_book_not_trade(self):
        """Snapshot → negative delta (fill) → trade: book only decremented once."""
        tf = KalshiTransform()

        # Snapshot: YES 50c x100, NO 40c x200
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
        ), T, conn_id="c1")

        # Delta: -50 on NO side at 40c (fill consumed 50 contracts)
        tf(_delta_frame(TICKER, "0.4000", "-50.00", "no", seq=5), T + 1, conn_id="c1")

        # Trade: YES taker at 60c (=100-40), size 50 — same fill the delta described
        tf(_trade_frame(TICKER, "0.6000", "50.00", "yes", seq=6), T + 1, conn_id="c1")

        book = tf._books[TICKER]
        # NO book decremented once (by delta), not twice
        assert book.no_book[40] == 150  # 200 - 50
        assert book.yes_book[50] == 100  # untouched

    def test_sequence_snapshot_delta_trade(self):
        """Full sequence: snapshot → placement delta → fill delta → trade."""
        tf = KalshiTransform()

        # Snapshot: YES 50c x100, NO 40c x200
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
        ), T, conn_id="c1")

        # Delta: add YES 55c x500 (placement)
        tf(_delta_frame(TICKER, "0.5500", "500.00", "yes", seq=5), T + 1, conn_id="c1")

        # Delta: remove YES 55c x200 (fill consumed by NO taker)
        tf(_delta_frame(TICKER, "0.5500", "-200.00", "yes", seq=6), T + 2, conn_id="c1")

        # Trade: NO taker at YES price 55c, size 200
        result = tf(_trade_frame(TICKER, "0.5500", "200.00", "no", seq=7), T + 2, conn_id="c1")

        book = tf._books[TICKER]
        assert book.yes_book[55] == 300   # 500 - 200 (from delta only)
        assert book.yes_book[50] == 100   # untouched
        assert book.no_book[40] == 200    # untouched
        # Trade produces event but no depth row
        assert len(result.events) == 1
        assert len(result.depth_rows) == 0


class TestReconnectReplay:
    """End-to-end replay with WS reconnections via conn_id changes."""

    def test_reconnect_clears_books_and_emits_invalidated(self):
        tf = KalshiTransform()

        # Connection A: seed two tickers
        tf(_snapshot_frame("TICKER-A",
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
        ), T, conn_id="conn-a")
        tf(_snapshot_frame("TICKER-B",
            yes_levels=[["0.3000", "50.00"]],
            no_levels=[["0.6000", "80.00"]],
        ), T + 1, conn_id="conn-a")

        assert len(tf._books) == 2

        # Reconnect: first frame with new conn_id
        result = tf(_snapshot_frame("TICKER-A",
            yes_levels=[["0.7000", "999.00"]],
            no_levels=[["0.2000", "888.00"]],
        ), T + 10, conn_id="conn-b")

        # Should have emitted BookInvalidated for both tickers
        invalidated = [e for e in result.events if isinstance(e, BookInvalidated)]
        invalidated_tickers = {e.market_ticker for e in invalidated}
        assert invalidated_tickers == {"TICKER-A", "TICKER-B"}

        # TICKER-A has fresh book from new snapshot
        assert tf._books["TICKER-A"].yes_book[70] == 999
        # TICKER-B was cleared and has no new snapshot
        assert "TICKER-B" not in tf._books

    def test_reconnect_full_replay(self):
        """Simulate a full bronze replay: conn-a activity → reconnect → conn-b activity."""
        tf = KalshiTransform()

        # --- Connection A ---
        # Snapshot
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "1000.00"]],
            no_levels=[["0.4000", "2000.00"]],
        ), T, conn_id="conn-a")

        # Placement delta
        tf(_delta_frame(TICKER, "0.5500", "500.00", "yes", seq=2), T + 1, conn_id="conn-a")

        # Fill delta + trade
        tf(_delta_frame(TICKER, "0.5500", "-200.00", "yes", seq=3), T + 2, conn_id="conn-a")
        result_trade = tf(_trade_frame(TICKER, "0.5500", "200.00", "no", seq=4), T + 2, conn_id="conn-a")

        # Verify conn-a state
        book_a = tf._books[TICKER]
        assert book_a.yes_book[50] == 1000
        assert book_a.yes_book[55] == 300  # 500 - 200
        assert book_a.no_book[40] == 2000
        assert len(result_trade.events) == 1
        assert isinstance(result_trade.events[0], TradeEvent)

        # --- Reconnect to Connection B ---
        result_reconnect = tf(_snapshot_frame(TICKER,
            yes_levels=[["0.6000", "777.00"]],
            no_levels=[["0.3500", "333.00"]],
        ), T + 100, conn_id="conn-b")

        # BookInvalidated emitted
        invalidated = [e for e in result_reconnect.events if isinstance(e, BookInvalidated)]
        assert len(invalidated) == 1
        assert invalidated[0].market_ticker == TICKER

        # Fresh book from conn-b snapshot — no carryover from conn-a
        book_b = tf._books[TICKER]
        assert book_b.yes_book == {60: 777}
        assert book_b.no_book == {35: 333}
        assert 50 not in book_b.yes_book
        assert 55 not in book_b.yes_book

        # More activity on conn-b
        tf(_delta_frame(TICKER, "0.6000", "-100.00", "yes", seq=2), T + 101, conn_id="conn-b")
        tf(_trade_frame(TICKER, "0.6000", "100.00", "no", seq=3), T + 101, conn_id="conn-b")

        assert tf._books[TICKER].yes_book[60] == 677  # 777 - 100

    def test_delta_for_unknown_ticker_after_reconnect(self):
        """After reconnect, delta for ticker without new snapshot is dropped."""
        tf = KalshiTransform()

        # Conn-a: snapshot for TICKER-A
        tf(_snapshot_frame("TICKER-A",
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
        ), T, conn_id="conn-a")

        # Reconnect to conn-b with snapshot for TICKER-A only
        tf(_snapshot_frame("TICKER-A",
            yes_levels=[["0.6000", "500.00"]],
            no_levels=[["0.3000", "300.00"]],
        ), T + 10, conn_id="conn-b")

        # Delta for TICKER-B (never got a snapshot on conn-b)
        result = tf(_delta_frame("TICKER-B", "0.5000", "100.00", "yes", seq=5),
                     T + 11, conn_id="conn-b")

        # Should produce nothing — no book for TICKER-B
        assert len(result.depth_rows) == 0
        assert len(result.events) == 0
        assert "TICKER-B" not in tf._books

    def test_multiple_reconnects(self):
        """Multiple reconnects in sequence — each clears and resets."""
        tf = KalshiTransform()

        # Conn-a
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
        ), T, conn_id="conn-a")
        assert tf._books[TICKER].yes_book[50] == 100

        # Conn-b
        result_b = tf(_snapshot_frame(TICKER,
            yes_levels=[["0.6000", "200.00"]],
            no_levels=[["0.3000", "300.00"]],
        ), T + 10, conn_id="conn-b")
        assert len([e for e in result_b.events if isinstance(e, BookInvalidated)]) == 1
        assert tf._books[TICKER].yes_book == {60: 200}

        # Conn-c
        result_c = tf(_snapshot_frame(TICKER,
            yes_levels=[["0.7000", "300.00"]],
            no_levels=[["0.2000", "400.00"]],
        ), T + 20, conn_id="conn-c")
        assert len([e for e in result_c.events if isinstance(e, BookInvalidated)]) == 1
        assert tf._books[TICKER].yes_book == {70: 300}
        assert 60 not in tf._books[TICKER].yes_book


class TestSidFiltering:
    """Deltas from a stale subscription (wrong sid) must be ignored."""

    def test_stale_sid_delta_ignored(self):
        """After re-snapshot with new sid, old-sid deltas are dropped."""
        tf = KalshiTransform()

        # Initial snapshot on sid=1
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
            sid=1, seq=1,
        ), T, conn_id="c1")
        assert tf._books[TICKER].yes_book[50] == 100

        # Re-snapshot on sid=2 (periodic re-subscribe)
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
            sid=2, seq=1,
        ), T + 10, conn_id="c1")
        assert tf._books[TICKER].sid == 2

        # Stale delta from old sid=1 arrives late — should be ignored
        result = tf(_delta_frame(TICKER, "0.5000", "9999.00", "yes", sid=1, seq=99),
                    T + 11, conn_id="c1")
        assert tf._books[TICKER].yes_book[50] == 100  # unchanged
        assert len(result.depth_rows) == 0

    def test_matching_sid_delta_applied(self):
        """Deltas matching current sid are applied normally."""
        tf = KalshiTransform()

        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
            sid=2, seq=1,
        ), T, conn_id="c1")

        result = tf(_delta_frame(TICKER, "0.5000", "50.00", "yes", sid=2, seq=2),
                    T + 1, conn_id="c1")
        assert tf._books[TICKER].yes_book[50] == 150
        assert len(result.depth_rows) == 1

    def test_interleaved_sids_only_current_applied(self):
        """Simulates the bug from notebook: interleaving sid=1 and sid=2 deltas."""
        tf = KalshiTransform()

        # Snapshot on sid=2
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
            sid=2, seq=1,
        ), T, conn_id="c1")

        # Interleaved: sid=1 (stale), sid=2 (current), sid=1 (stale)
        tf(_delta_frame(TICKER, "0.5000", "1000.00", "yes", sid=1, seq=50), T + 1, conn_id="c1")
        tf(_delta_frame(TICKER, "0.5000", "10.00", "yes", sid=2, seq=2), T + 2, conn_id="c1")
        tf(_delta_frame(TICKER, "0.5000", "5000.00", "yes", sid=1, seq=51), T + 3, conn_id="c1")

        # Only sid=2 delta applied: 100 + 10 = 110
        assert tf._books[TICKER].yes_book[50] == 110


class TestCrossedBookInvalidation:
    """When a delta causes a book to cross, the book is invalidated
    (removed) and a BookInvalidated event is emitted."""

    def test_crossed_book_is_invalidated(self):
        """Delta that causes crossing removes book and emits event."""
        tf = KalshiTransform()

        # Tight spread: bid=55, ask=56 (spread=1)
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5500", "100.00"]],
            no_levels=[["0.4400", "100.00"]],  # ask = 100-44 = 56
        ), T, conn_id="c1")
        assert tf._books[TICKER].spread == 1

        # Delta that pushes YES bid above ask → crossed
        result = tf(_delta_frame(TICKER, "0.5700", "50.00", "yes", seq=2), T + 1, conn_id="c1")

        # Book should be removed
        assert TICKER not in tf._books
        # BookInvalidated emitted
        assert any(isinstance(e, BookInvalidated) for e in result.events)
        # No depth row emitted for the crossed state
        assert len(result.depth_rows) == 0

    def test_crossed_book_recovers_on_next_snapshot(self):
        """After invalidation, a new snapshot re-establishes the book."""
        tf = KalshiTransform()

        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5500", "100.00"]],
            no_levels=[["0.4400", "100.00"]],
        ), T, conn_id="c1")

        # Force a cross
        tf(_delta_frame(TICKER, "0.5700", "50.00", "yes", seq=2), T + 1, conn_id="c1")
        assert TICKER not in tf._books

        # New snapshot recovers
        result = tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "200.00"]],
            no_levels=[["0.4000", "300.00"]],
            seq=100,
        ), T + 10, conn_id="c1")

        assert TICKER in tf._books
        assert tf._books[TICKER].spread == 10
        assert len(result.depth_rows) == 1

    def test_non_crossing_delta_still_works(self):
        """Normal deltas that don't cross still produce depth rows."""
        tf = KalshiTransform()

        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "100.00"]],
            no_levels=[["0.4000", "200.00"]],
        ), T, conn_id="c1")

        result = tf(_delta_frame(TICKER, "0.5000", "50.00", "yes", seq=2), T + 1, conn_id="c1")
        assert TICKER in tf._books
        assert len(result.depth_rows) == 1
        assert result.depth_rows[0]["spread"] == 10


class TestBookInvariantsDuringReplay:
    """Validate that book state never violates invariants (negative sizes,
    crossed books, out-of-range prices) during realistic replay sequences.

    These tests catch the double-counting bug: if trades modified the book
    AND deltas also decremented for fills, sizes would go negative.
    """

    def test_no_negative_sizes_after_fill_delta_and_trade(self):
        """The old double-counting bug: fill delta + trade decrement = negative size.
        With the fix, only the delta decrements — sizes stay non-negative."""
        tf = KalshiTransform()

        # Snapshot: NO 60c x100 (small resting order)
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.3000", "500.00"]],
            no_levels=[["0.6000", "100.00"]],
        ), T, conn_id="c1")

        # Fill delta: -100 on NO at 60c (entire level consumed)
        tf(_delta_frame(TICKER, "0.6000", "-100.00", "no", seq=2), T + 1, conn_id="c1")

        # Trade: YES taker at 40c (100-60=40), size 100
        tf(_trade_frame(TICKER, "0.4000", "100.00", "yes", seq=3), T + 1, conn_id="c1")

        book = tf._books[TICKER]
        errors = book.validate()
        neg_errors = [e for e in errors if e.check == "negative_size"]
        assert neg_errors == [], f"Negative sizes found: {neg_errors}"
        # Level should be fully cleared by the delta
        assert 60 not in book.no_book

    def test_no_negative_sizes_repeated_fills(self):
        """Multiple fills in a row — each has a delta + trade pair."""
        tf = KalshiTransform()

        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "1000.00"]],
            no_levels=[["0.4000", "500.00"]],
        ), T, conn_id="c1")

        # 5 consecutive fills, each 100 contracts from YES at 50c
        for i in range(5):
            seq_base = 10 + i * 2
            t = T + 1 + i
            # Delta: -100 from YES at 50c
            tf(_delta_frame(TICKER, "0.5000", "-100.00", "yes", seq=seq_base), t, conn_id="c1")
            # Trade: NO taker at 50c
            tf(_trade_frame(TICKER, "0.5000", "100.00", "no", seq=seq_base + 1), t, conn_id="c1")

        book = tf._books[TICKER]
        errors = book.validate()
        neg_errors = [e for e in errors if e.check == "negative_size"]
        assert neg_errors == [], f"Negative sizes: {neg_errors}"
        assert book.yes_book[50] == 500  # 1000 - 5*100

    def test_no_crossed_book_after_normal_activity(self):
        """Snapshot with valid spread → deltas + trades → spread stays valid."""
        tf = KalshiTransform()

        # Spread = 100-40-50 = 10c (healthy)
        tf(_snapshot_frame(TICKER,
            yes_levels=[["0.5000", "200.00"], ["0.4500", "300.00"]],
            no_levels=[["0.4000", "200.00"], ["0.3500", "100.00"]],
        ), T, conn_id="c1")

        book = tf._books[TICKER]
        assert book.spread == 10  # ask=60, bid=50

        # Add YES at 55c (tightens spread to 5c)
        tf(_delta_frame(TICKER, "0.5500", "100.00", "yes", seq=2), T + 1, conn_id="c1")
        assert book.spread == 5  # ask=60, bid=55

        # Fill: delta removes YES at 55c, trade matches
        tf(_delta_frame(TICKER, "0.5500", "-100.00", "yes", seq=3), T + 2, conn_id="c1")
        tf(_trade_frame(TICKER, "0.5500", "100.00", "no", seq=4), T + 2, conn_id="c1")

        # Spread should widen back to 10c, not cross
        assert book.spread == 10
        errors = book.validate()
        crossed = [e for e in errors if e.check == "crossed_book"]
        assert crossed == [], f"Crossed book: {crossed}"

    def test_intensive_replay_all_invariants_hold(self):
        """Large replay: interleaved snapshots, deltas, trades, reconnects.
        Validates every book after every frame."""
        tf = KalshiTransform()
        t = T

        frames = [
            # --- conn-a: initial setup ---
            ("snap", "conn-a", _snapshot_frame(TICKER,
                yes_levels=[["0.5000", "1000.00"], ["0.4000", "500.00"]],
                no_levels=[["0.4500", "800.00"], ["0.3000", "200.00"]],
            )),
            # Placements
            ("delta", "conn-a", _delta_frame(TICKER, "0.5200", "300.00", "yes", seq=2)),
            ("delta", "conn-a", _delta_frame(TICKER, "0.4200", "600.00", "no", seq=3)),
            # Fill on YES side (NO taker)
            ("delta", "conn-a", _delta_frame(TICKER, "0.5200", "-150.00", "yes", seq=4)),
            ("trade", "conn-a", _trade_frame(TICKER, "0.5200", "150.00", "no", seq=5)),
            # Fill on NO side (YES taker)
            ("delta", "conn-a", _delta_frame(TICKER, "0.4500", "-200.00", "no", seq=6)),
            ("trade", "conn-a", _trade_frame(TICKER, "0.5500", "200.00", "yes", seq=7)),
            # Cancellation
            ("delta", "conn-a", _delta_frame(TICKER, "0.4000", "-500.00", "yes", seq=8)),
            # More placements
            ("delta", "conn-a", _delta_frame(TICKER, "0.4800", "1000.00", "yes", seq=9)),
            # --- reconnect to conn-b ---
            ("snap", "conn-b", _snapshot_frame(TICKER,
                yes_levels=[["0.6000", "400.00"], ["0.5500", "200.00"]],
                no_levels=[["0.3500", "300.00"], ["0.2000", "100.00"]],
            )),
            # Activity on conn-b
            ("delta", "conn-b", _delta_frame(TICKER, "0.6000", "-50.00", "yes", seq=2)),
            ("trade", "conn-b", _trade_frame(TICKER, "0.6000", "50.00", "no", seq=3)),
            ("delta", "conn-b", _delta_frame(TICKER, "0.5800", "500.00", "yes", seq=4)),
            ("delta", "conn-b", _delta_frame(TICKER, "0.3500", "-100.00", "no", seq=5)),
            ("trade", "conn-b", _trade_frame(TICKER, "0.6500", "100.00", "yes", seq=6)),
        ]

        violation_count = 0
        for i, (kind, conn_id, frame) in enumerate(frames):
            t += 1
            result = tf(frame, t, conn_id=conn_id)

            # Validate every book after every frame
            for ticker, book in tf._books.items():
                errors = book.validate()
                neg = [e for e in errors if e.check == "negative_size"]
                oor = [e for e in errors if e.check == "price_out_of_range"]
                if neg or oor:
                    violation_count += 1
                    raise AssertionError(
                        f"Frame {i} ({kind}): {ticker} has violations: {neg + oor}"
                    )

        assert violation_count == 0, f"{violation_count} frames had violations"

        # Final state check after conn-b activity
        book = tf._books[TICKER]
        assert book.yes_book[60] == 350   # 400 - 50
        assert book.yes_book[55] == 200   # untouched
        assert book.yes_book[58] == 500   # new placement
        assert book.no_book[35] == 200    # 300 - 100
        assert book.no_book[20] == 100    # untouched


# ── Real bronze replay ──────────────────────────────────────────────

BUCKET = "prediction-markets-data"
SOURCE = "kalshi_ws"
CHANNELS = ("orderbook_snapshot", "orderbook_delta", "trade")


def _load_bronze(date: str) -> list[dict]:
    """Load bronze records for a date from S3 (merged preferred)."""
    s3 = boto3.client("s3")
    records = []
    for channel in CHANNELS:
        key = f"bronze_merged/{SOURCE}/{channel}/date={date}/merged.jsonl.gz"
        try:
            resp = s3.get_object(Bucket=BUCKET, Key=key)
        except s3.exceptions.NoSuchKey:
            continue
        data = gzip.decompress(resp["Body"].read()).decode()
        for line in data.split("\n"):
            if line.strip():
                records.append(json.loads(line))
    records.sort(key=lambda r: r.get("t_receipt", 0.0))
    return records


@pytest.mark.skipif(
    os.environ.get("SKIP_S3_TESTS", "1") == "1",
    reason="Set SKIP_S3_TESTS=0 to run real bronze replay tests",
)
class TestBronzeReplayInvariants:
    """Replay real bronze data through KalshiTransform and validate
    book invariants after every frame.

    Run with: SKIP_S3_TESTS=0 python -m pytest v2/tests/test_transform.py -k bronze -v
    """

    DATE = "2026-04-27"  # smallest date (~13 MB, 671K records)

    def test_no_negative_sizes_or_bad_prices(self):
        """Replay all bronze records; no book should ever have negative
        sizes or out-of-range prices."""
        records = _load_bronze(self.DATE)
        assert len(records) > 100_000, f"Expected >100K records, got {len(records)}"

        tf = KalshiTransform()
        negative_count = 0
        oor_count = 0
        frames_processed = 0
        sample_violations = []

        for rec in records:
            frame = rec.get("frame", {})
            t_receipt = rec.get("t_receipt", 0.0)
            conn_id = rec.get("conn_id")
            tf(frame, t_receipt, conn_id=conn_id)
            frames_processed += 1

            for ticker, book in tf._books.items():
                for price, size in book.yes_book.items():
                    if size < 0:
                        negative_count += 1
                        if len(sample_violations) < 10:
                            sample_violations.append(
                                f"yes {ticker} {price}c size={size}")
                    if price < 1 or price > 99:
                        oor_count += 1
                for price, size in book.no_book.items():
                    if size < 0:
                        negative_count += 1
                        if len(sample_violations) < 10:
                            sample_violations.append(
                                f"no {ticker} {price}c size={size}")
                    if price < 1 or price > 99:
                        oor_count += 1

        print(f"\nProcessed {frames_processed:,} frames across {len(tf._books)} tickers")
        print(f"Negative sizes: {negative_count}")
        print(f"Out-of-range prices: {oor_count}")
        if sample_violations:
            print(f"Samples: {sample_violations}")

        assert negative_count == 0, \
            f"{negative_count} negative sizes found: {sample_violations}"
        assert oor_count == 0, \
            f"{oor_count} out-of-range prices found"

    def test_crossed_book_rate(self):
        """Replay all bronze, track crossed book rate at each depth row.
        With the fix, crossed books should be rare (< 5% of depth rows)."""
        records = _load_bronze(self.DATE)
        tf = KalshiTransform()

        total_depth_rows = 0
        crossed_count = 0
        crossed_by_ticker = Counter()

        for rec in records:
            frame = rec.get("frame", {})
            t_receipt = rec.get("t_receipt", 0.0)
            conn_id = rec.get("conn_id")
            result = tf(frame, t_receipt, conn_id=conn_id)

            for row in result.depth_rows:
                total_depth_rows += 1
                spread = row.get("spread")
                if spread is not None and spread < 0:
                    crossed_count += 1
                    crossed_by_ticker[row["market_ticker"]] += 1

        crossed_pct = crossed_count / max(total_depth_rows, 1) * 100
        print(f"\nDepth rows: {total_depth_rows:,}")
        print(f"Crossed: {crossed_count:,} ({crossed_pct:.1f}%)")
        if crossed_by_ticker:
            print(f"Top crossed tickers:")
            for t, c in crossed_by_ticker.most_common(5):
                print(f"  {t}: {c:,}")

        # With the fix, we expect very low crossed rate
        # (some crossing is normal due to message ordering)
        assert crossed_pct < 5.0, \
            f"Crossed book rate {crossed_pct:.1f}% exceeds 5% threshold"

    def test_trade_events_preserved(self):
        """All trades in bronze should produce TradeEvent — no trades lost."""
        records = _load_bronze(self.DATE)
        tf = KalshiTransform()

        bronze_trades = sum(
            1 for r in records
            if r.get("frame", {}).get("type") == "trade"
            and r.get("frame", {}).get("msg", {}).get("yes_price_dollars") is not None
        )

        trade_events = 0
        for rec in records:
            frame = rec.get("frame", {})
            t_receipt = rec.get("t_receipt", 0.0)
            conn_id = rec.get("conn_id")
            result = tf(frame, t_receipt, conn_id=conn_id)
            trade_events += sum(1 for e in result.events if isinstance(e, TradeEvent))

        print(f"\nBronze trade frames: {bronze_trades:,}")
        print(f"TradeEvents emitted: {trade_events:,}")

        assert trade_events == bronze_trades, \
            f"Lost {bronze_trades - trade_events} trades"
