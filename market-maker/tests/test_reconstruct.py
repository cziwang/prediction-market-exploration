"""Tests for batch LOB reconstruction."""

import polars as pl
import pytest

from src.reconstruct import BatchBook, reconstruct_market, verify_boundary


class TestBatchBook:
    def test_load_snapshot(self):
        levels = pl.DataFrame({
            "side": ["yes", "yes", "no"],
            "price_cents": [55, 50, 44],
            "qty": [100, 200, 150],
        })
        book = BatchBook()
        book.load_snapshot(levels)
        assert book.sides["yes"] == {55: 100, 50: 200}
        assert book.sides["no"] == {44: 150}

    def test_apply_delta_add(self):
        book = BatchBook()
        book.apply_delta("yes", 55, 100)
        assert book.sides["yes"][55] == 100

    def test_apply_delta_clamp_zero(self):
        book = BatchBook()
        book.sides["yes"][55] = 30
        book.apply_delta("yes", 55, -50)
        # max(0, 30 + (-50)) = 0 → removed
        assert 55 not in book.sides["yes"]

    def test_emit(self):
        book = BatchBook()
        book.sides["yes"] = {55: 100}
        book.sides["no"] = {44: 200}
        rows = book.emit("TEST", 1000)
        assert len(rows) == 2
        assert all(r["market_ticker"] == "TEST" for r in rows)
        assert all(r["ts"] == 1000 for r in rows)


class TestVerifyBoundary:
    def test_matching(self):
        book = BatchBook()
        book.sides["yes"] = {55: 100}
        book.sides["no"] = {44: 200}

        snap = pl.DataFrame({
            "side": ["yes", "no"],
            "price_cents": [55, 44],
            "qty": [100, 200],
        })
        assert verify_boundary(book, snap, "T", 0) is True

    def test_drifted(self):
        book = BatchBook()
        book.sides["yes"] = {55: 100}
        book.sides["no"] = {44: 200}

        snap = pl.DataFrame({
            "side": ["yes", "no"],
            "price_cents": [55, 44],
            "qty": [99, 200],  # off by 1
        })
        assert verify_boundary(book, snap, "T", 0) is False


class TestReconstructMarket:
    def test_simple_reconstruction(self):
        """Two snapshots, one delta in between."""
        snapshots = pl.DataFrame({
            "ts": [100, 100, 200, 200],
            "side": ["yes", "no", "yes", "no"],
            "price_cents": [55, 44, 55, 44],
            "qty": [100, 150, 110, 150],
        })

        deltas = pl.DataFrame({
            "ts": [150],
            "side": ["yes"],
            "price_cents": [55],
            "delta": [10],
        })

        rows = reconstruct_market("TEST", snapshots, deltas)

        # Should have:
        # 1. Snapshot S1 emit: 2 levels (ts=100)
        # 2. Delta at ts=150: 2 levels (yes=110, no=150)
        # 3. Snapshot S2 emit: 2 levels (ts=200)
        assert len(rows) >= 4  # at least snapshot + delta emissions

        # After delta at ts=150, yes@55 should be 110.
        delta_rows = [r for r in rows if r["ts"] == 150]
        yes_row = [r for r in delta_rows if r["side"] == "yes" and r["price_cents"] == 55]
        assert len(yes_row) == 1
        assert yes_row[0]["qty"] == 110

    def test_delta_removes_level(self):
        snapshots = pl.DataFrame({
            "ts": [100, 100],
            "side": ["yes", "no"],
            "price_cents": [55, 44],
            "qty": [50, 100],
        })

        deltas = pl.DataFrame({
            "ts": [150],
            "side": ["yes"],
            "price_cents": [55],
            "delta": [-50],
        })

        rows = reconstruct_market("TEST", snapshots, deltas)

        # After delta: yes@55 removed, only no@44 remains.
        delta_rows = [r for r in rows if r["ts"] == 150]
        assert len(delta_rows) == 1
        assert delta_rows[0]["side"] == "no"

    def test_no_snapshots(self):
        snapshots = pl.DataFrame({
            "ts": pl.Series([], dtype=pl.Int64),
            "side": pl.Series([], dtype=pl.Utf8),
            "price_cents": pl.Series([], dtype=pl.Int32),
            "qty": pl.Series([], dtype=pl.Int32),
        })
        deltas = pl.DataFrame({
            "ts": [100],
            "side": ["yes"],
            "price_cents": [55],
            "delta": [10],
        })
        rows = reconstruct_market("TEST", snapshots, deltas)
        assert rows == []
