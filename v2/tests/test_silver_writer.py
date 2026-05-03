"""Tests for v2 SilverWriter: explicit schemas, timestamps, sorting, min-rows guard."""

from __future__ import annotations

import asyncio
import io
from unittest.mock import MagicMock

import pyarrow.parquet as pq
import pytest

from v2.app.events import (
    BookInvalidated,
    MMCircuitBreakerEvent,
    MMFillEvent,
    MMOrderEvent,
    MMQuoteEvent,
    MMReconcileEvent,
    OrderBookUpdate,
    TradeEvent,
)
from v2.app.services.silver_writer import SCHEMAS, SilverWriter, _prepare_rows


# ---------------------------------------------------------------------------
# _prepare_rows
# ---------------------------------------------------------------------------

class TestPrepareRows:
    def test_converts_t_receipt_to_ns(self):
        events = [OrderBookUpdate(t_receipt=1714500000.123, market_ticker="T",
                                  bid_yes=50, ask_yes=55, bid_size=100, ask_size=100)]
        rows = _prepare_rows(events)
        assert "t_receipt_ns" in rows[0]
        assert "t_receipt" not in rows[0]
        assert abs(rows[0]["t_receipt_ns"] - 1714500000123000000) < 1000  # sub-microsecond precision

    def test_converts_t_exchange_to_ns(self):
        events = [OrderBookUpdate(t_receipt=1.0, market_ticker="T",
                                  bid_yes=50, ask_yes=55, bid_size=100, ask_size=100,
                                  t_exchange=1714500000.5)]
        rows = _prepare_rows(events)
        assert "t_exchange_ns" in rows[0]
        assert "t_exchange" not in rows[0]
        assert abs(rows[0]["t_exchange_ns"] - 1714500000500000000) < 1000

    def test_t_exchange_none_stays_none(self):
        events = [OrderBookUpdate(t_receipt=1.0, market_ticker="T",
                                  bid_yes=50, ask_yes=55, bid_size=100, ask_size=100,
                                  t_exchange=None)]
        rows = _prepare_rows(events)
        assert rows[0]["t_exchange_ns"] is None

    def test_sorts_by_t_receipt_ns(self):
        events = [
            OrderBookUpdate(t_receipt=3.0, market_ticker="T",
                            bid_yes=50, ask_yes=55, bid_size=100, ask_size=100),
            OrderBookUpdate(t_receipt=1.0, market_ticker="T",
                            bid_yes=50, ask_yes=55, bid_size=100, ask_size=100),
            OrderBookUpdate(t_receipt=2.0, market_ticker="T",
                            bid_yes=50, ask_yes=55, bid_size=100, ask_size=100),
        ]
        rows = _prepare_rows(events)
        timestamps = [r["t_receipt_ns"] for r in rows]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# Schema coverage
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_all_event_types_have_schemas(self):
        expected = {
            "OrderBookUpdate", "TradeEvent", "BookInvalidated",
            "MMQuoteEvent", "MMOrderEvent", "MMFillEvent",
            "MMReconcileEvent", "MMCircuitBreakerEvent",
            "OrderBookDepth",
        }
        assert set(SCHEMAS.keys()) == expected

    def test_all_schemas_start_with_t_receipt_ns(self):
        for name, schema in SCHEMAS.items():
            assert schema.field(0).name == "t_receipt_ns", f"{name} first field should be t_receipt_ns"
            assert schema.field(0).type == "int64"


# ---------------------------------------------------------------------------
# SilverWriter integration
# ---------------------------------------------------------------------------

class TestSilverWriter:
    def _make_writer(self, min_rows=1):
        mock_s3 = MagicMock()
        mock_s3.put_object = MagicMock()
        writer = SilverWriter(
            source="test",
            flush_seconds=9999,  # won't auto-flush
            min_rows=min_rows,
            bucket="test-bucket",
            version=3,
            s3_client=mock_s3,
        )
        return writer, mock_s3

    @pytest.mark.asyncio
    async def test_flush_writes_parquet_with_correct_schema(self):
        writer, mock_s3 = self._make_writer()

        event = OrderBookUpdate(
            t_receipt=1714500000.5,
            market_ticker="KXNBAPTS-TEST",
            bid_yes=50, ask_yes=55,
            bid_size=100, ask_size=200,
            t_exchange=1714500000.4,
            sid=1, seq=42,
        )

        async with writer:
            await writer.emit(event)
            key = await writer._flush_type("OrderBookUpdate")

        assert key is not None
        call_args = mock_s3.put_object.call_args
        body = call_args.kwargs.get("Body") or call_args[1].get("Body")
        table = pq.read_table(io.BytesIO(body))

        assert "t_receipt_ns" in table.column_names
        assert "t_exchange_ns" in table.column_names
        assert "t_receipt" not in table.column_names
        assert "t_exchange" not in table.column_names
        assert "sid" in table.column_names
        assert "seq" in table.column_names
        assert table.num_rows == 1
        assert table.column("bid_yes")[0].as_py() == 50
        assert table.column("market_ticker")[0].as_py() == "KXNBAPTS-TEST"
        assert table.column("sid")[0].as_py() == 1
        assert table.column("seq")[0].as_py() == 42

    @pytest.mark.asyncio
    async def test_min_rows_guard_skips_small_flushes(self):
        writer, mock_s3 = self._make_writer(min_rows=10)

        event = OrderBookUpdate(
            t_receipt=1.0, market_ticker="T",
            bid_yes=50, ask_yes=55, bid_size=100, ask_size=100,
        )

        async with writer:
            await writer.emit(event)
            # Manual flush should be skipped (only 1 row, min_rows=10)
            key = await writer._flush_type("OrderBookUpdate")
            assert key is None
            # Buffer should still have the event
            assert len(writer._buffers["OrderBookUpdate"]) == 1

        # But __aexit__ (drain) should flush regardless
        assert mock_s3.put_object.called

    @pytest.mark.asyncio
    async def test_s3_key_format(self):
        writer, mock_s3 = self._make_writer()

        event = TradeEvent(t_receipt=1.0, market_ticker="T", side="yes", price=50, size=10)

        async with writer:
            await writer.emit(event)
            key = await writer._flush_type("TradeEvent")

        assert key is not None
        assert "silver/test/TradeEvent/" in key
        assert "/v=3/" in key
        assert key.endswith(".parquet")

    @pytest.mark.asyncio
    async def test_nullable_fields(self):
        writer, mock_s3 = self._make_writer()

        event = MMQuoteEvent(
            t_receipt=1.0, market_ticker="T",
            bid_price=None, ask_price=None,
            book_bid=50, book_ask=55, spread=5, position=0,
            reason_no_bid="dead_market", reason_no_ask=None,
        )

        async with writer:
            await writer.emit(event)
            key = await writer._flush_type("MMQuoteEvent")

        assert key is not None
        call_args = mock_s3.put_object.call_args
        body = call_args.kwargs.get("Body") or call_args[1].get("Body")
        table = pq.read_table(io.BytesIO(body))
        assert table.column("bid_price")[0].as_py() is None
        assert table.column("reason_no_bid")[0].as_py() == "dead_market"
        assert table.column("reason_no_ask")[0].as_py() is None
