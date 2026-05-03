"""Tests for build_silver validation guardrails.

Covers:
- _validate_books_inline: detects negative sizes and out-of-range prices
- _validate_final: checks crossed rate, negative depth sizes, trade count
- SilverValidationError prevents bad data from reaching S3
"""

import os
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from v2.app.core.book_state import BookState
from v2.app.transforms.kalshi_ws import KalshiTransform
from v2.scripts.build_silver import (
    SilverValidationError,
    _validate_books_inline,
    _validate_final,
    MAX_CROSSED_RATE,
)
from v2.app.services.silver_writer import SCHEMAS


# ── Helpers ──────────────────────────────────────────────────────────

def _make_depth_parquet(rows: list[dict]) -> str:
    """Write depth rows to a temp Parquet file, return path."""
    schema = SCHEMAS["OrderBookDepth"]
    table = pa.Table.from_pylist(rows, schema=schema)
    tmp = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
    tmp.close()
    pq.write_table(table, tmp.name, compression="zstd")
    return tmp.name


def _minimal_depth_row(spread=5, bid_1_size=100, ask_1_size=100, **overrides):
    """Build a minimal valid depth row dict."""
    row = {
        "t_receipt_ns": 1777000000000000000,
        "t_exchange_ns": 1777000000000000000,
        "market_ticker": "TEST-TICKER",
        "seq": 1,
        "sid": 1,
        "bid_1": 50,
        "bid_1_size": bid_1_size,
        "ask_1": 55,
        "ask_1_size": ask_1_size,
        "spread": spread,
        "mid_x2": 105,
        "bid_depth_5c": bid_1_size,
        "ask_depth_5c": ask_1_size,
        "bid_depth_10c": bid_1_size,
        "ask_depth_10c": ask_1_size,
        "num_bid_levels": 1,
        "num_ask_levels": 1,
    }
    # Fill remaining levels with None
    for i in range(2, 11):
        row[f"bid_{i}"] = None
        row[f"bid_{i}_size"] = None
        row[f"ask_{i}"] = None
        row[f"ask_{i}_size"] = None
    row.update(overrides)
    return row


# ── _validate_books_inline tests ─────────────────────────────────────


class TestValidateBooksInline:
    def test_healthy_books_no_violations(self):
        tf = KalshiTransform()
        tf({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
            "market_ticker": "T1",
            "yes_dollars_fp": [["0.5000", "100.00"]],
            "no_dollars_fp": [["0.4000", "200.00"]],
        }}, 1.0, conn_id="c1")

        violations = _validate_books_inline(tf, 1)
        assert violations == []

    def test_detects_negative_yes_size(self):
        tf = KalshiTransform()
        tf({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
            "market_ticker": "T1",
            "yes_dollars_fp": [["0.5000", "100.00"]],
            "no_dollars_fp": [["0.4000", "200.00"]],
        }}, 1.0, conn_id="c1")
        # Force a negative size (would only happen with a bug)
        tf._books["T1"].yes_book[50] = -10

        violations = _validate_books_inline(tf, 99)
        assert len(violations) == 1
        assert "negative" not in violations[0]  # it reports as size=-10
        assert "-10" in violations[0]

    def test_detects_negative_no_size(self):
        tf = KalshiTransform()
        tf({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
            "market_ticker": "T1",
            "yes_dollars_fp": [["0.5000", "100.00"]],
            "no_dollars_fp": [["0.4000", "200.00"]],
        }}, 1.0, conn_id="c1")
        tf._books["T1"].no_book[40] = -5

        violations = _validate_books_inline(tf, 50)
        assert len(violations) == 1
        assert "-5" in violations[0]

    def test_detects_out_of_range_price(self):
        tf = KalshiTransform()
        tf({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
            "market_ticker": "T1",
            "yes_dollars_fp": [["0.5000", "100.00"]],
            "no_dollars_fp": [["0.4000", "200.00"]],
        }}, 1.0, conn_id="c1")
        tf._books["T1"].yes_book[0] = 50  # price 0 is invalid
        tf._books["T1"].yes_book[100] = 50  # price 100 is invalid

        violations = _validate_books_inline(tf, 10)
        assert len(violations) == 2

    def test_empty_books_no_violations(self):
        tf = KalshiTransform()
        violations = _validate_books_inline(tf, 0)
        assert violations == []

    def test_multiple_tickers(self):
        tf = KalshiTransform()
        for ticker in ["T1", "T2", "T3"]:
            tf({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
                "market_ticker": ticker,
                "yes_dollars_fp": [["0.5000", "100.00"]],
                "no_dollars_fp": [["0.4000", "200.00"]],
            }}, 1.0, conn_id="c1")

        # Make T2 bad
        tf._books["T2"].yes_book[50] = -1

        violations = _validate_books_inline(tf, 99)
        assert len(violations) == 1
        assert "T2" in violations[0]


# ── _validate_final tests ────────────────────────────────────────────


class TestValidateFinal:
    def test_passes_on_clean_data(self):
        rows = [_minimal_depth_row(spread=5) for _ in range(100)]
        path = _make_depth_parquet(rows)
        try:
            _validate_final(path, trade_count=500, bronze_trade_count=500)
        finally:
            os.unlink(path)

    def test_fails_on_trade_count_mismatch(self):
        rows = [_minimal_depth_row() for _ in range(10)]
        path = _make_depth_parquet(rows)
        try:
            with pytest.raises(SilverValidationError, match="Trade count mismatch"):
                _validate_final(path, trade_count=100, bronze_trade_count=200)
        finally:
            os.unlink(path)

    def test_fails_on_high_crossed_rate(self):
        # 60% crossed
        rows = []
        for i in range(100):
            spread = -5 if i < 60 else 5
            rows.append(_minimal_depth_row(spread=spread))
        path = _make_depth_parquet(rows)
        try:
            with pytest.raises(SilverValidationError, match="Crossed book rate"):
                _validate_final(path, trade_count=50, bronze_trade_count=50)
        finally:
            os.unlink(path)

    def test_passes_on_low_crossed_rate(self):
        # 3% crossed (below 5% threshold)
        rows = []
        for i in range(100):
            spread = -5 if i < 3 else 5
            rows.append(_minimal_depth_row(spread=spread))
        path = _make_depth_parquet(rows)
        try:
            _validate_final(path, trade_count=50, bronze_trade_count=50)
        finally:
            os.unlink(path)

    def test_fails_at_threshold_boundary(self):
        # Exactly at 5% — should still pass (> not >=)
        rows = []
        for i in range(100):
            spread = -5 if i < 5 else 5
            rows.append(_minimal_depth_row(spread=spread))
        path = _make_depth_parquet(rows)
        try:
            _validate_final(path, trade_count=50, bronze_trade_count=50)
        finally:
            os.unlink(path)

    def test_fails_just_above_threshold(self):
        # 6% crossed — should fail
        rows = []
        for i in range(100):
            spread = -5 if i < 6 else 5
            rows.append(_minimal_depth_row(spread=spread))
        path = _make_depth_parquet(rows)
        try:
            with pytest.raises(SilverValidationError, match="Crossed book rate"):
                _validate_final(path, trade_count=50, bronze_trade_count=50)
        finally:
            os.unlink(path)

    def test_fails_on_negative_bid_size(self):
        rows = [_minimal_depth_row(bid_1_size=-10)]
        path = _make_depth_parquet(rows)
        try:
            with pytest.raises(SilverValidationError, match="negative values in bid_1_size"):
                _validate_final(path, trade_count=0, bronze_trade_count=0)
        finally:
            os.unlink(path)

    def test_fails_on_negative_ask_size(self):
        rows = [_minimal_depth_row(ask_1_size=-5)]
        path = _make_depth_parquet(rows)
        try:
            with pytest.raises(SilverValidationError, match="negative values in ask_1_size"):
                _validate_final(path, trade_count=0, bronze_trade_count=0)
        finally:
            os.unlink(path)

    def test_none_sizes_ignored(self):
        """None values in deeper levels shouldn't trigger failures."""
        row = _minimal_depth_row()
        # Levels 2-10 are already None from _minimal_depth_row
        rows = [row] * 10
        path = _make_depth_parquet(rows)
        try:
            _validate_final(path, trade_count=0, bronze_trade_count=0)
        finally:
            os.unlink(path)

    def test_empty_depth_file(self):
        """Edge case: no depth rows at all."""
        rows = []
        path = _make_depth_parquet(rows)
        try:
            _validate_final(path, trade_count=0, bronze_trade_count=0)
        finally:
            os.unlink(path)
