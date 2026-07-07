"""Tests for pm.replay.sources against the golden game fixture."""

import gzip
import json
from pathlib import Path

import pytest

from pm.replay.sources import LocalFileSource, SourceError, SourceRecord

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden"
GOLDEN_PBP = GOLDEN / "nba_pbp_0042500121.jsonl.gz"
GOLDEN_TRADES = GOLDEN / "kalshi_trades_0042500121.jsonl.gz"


class TestGoldenPbp:
    def test_reads_all_records(self) -> None:
        source = LocalFileSource(GOLDEN_PBP, "live_pbp")
        records = list(source.records())
        assert len(records) == 548  # full game, verified in fixture README

    def test_topic_and_key(self) -> None:
        source = LocalFileSource(GOLDEN_PBP, "live_pbp")
        first = next(source.records())
        assert first.topic == "nba.game_state"
        assert first.key == "0042500121"

    def test_timestamps_non_decreasing_in_file(self) -> None:
        # Golden PBP is merged output — sorted by action_number; receipt times
        # should also be non-decreasing since polls arrive in order.
        source = LocalFileSource(GOLDEN_PBP, "live_pbp")
        timestamps = [r.t_receipt_ns for r in source.records()]
        assert timestamps == sorted(timestamps)

    def test_raw_passthrough_is_valid_json(self) -> None:
        source = LocalFileSource(GOLDEN_PBP, "live_pbp")
        first = next(source.records())
        parsed = json.loads(first.raw)
        assert parsed["game_id"] == "0042500121"
        assert "frame" in parsed


class TestGoldenTrades:
    def test_reads_all_records(self) -> None:
        source = LocalFileSource(GOLDEN_TRADES, "trade")
        records = list(source.records())
        assert len(records) == 20376  # per fixture README

    def test_topic_and_key(self) -> None:
        source = LocalFileSource(GOLDEN_TRADES, "trade")
        first = next(source.records())
        assert first.topic == "kalshi.trades"
        assert "26APR18ATLNYK" in first.key  # golden game event code

    def test_t_receipt_ns_conversion(self) -> None:
        source = LocalFileSource(GOLDEN_TRADES, "trade")
        first = next(source.records())
        raw = json.loads(first.raw)
        assert first.t_receipt_ns == int(raw["t_receipt"] * 1_000_000_000)


class TestBadInput:
    def _write_gz(self, tmp_path: Path, lines: list[str]) -> Path:
        p = tmp_path / "bad.jsonl.gz"
        with gzip.open(p, "wt") as f:
            f.write("\n".join(lines))
        return p

    def test_unknown_channel_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="unknown channel"):
            LocalFileSource("whatever.jsonl.gz", "not_a_channel")

    def test_malformed_json_raises_source_error(self, tmp_path: Path) -> None:
        p = self._write_gz(tmp_path, ['{"t_receipt": 1.0, "game_id": "x"}', "{not json"])
        source = LocalFileSource(p, "live_pbp")
        with pytest.raises(SourceError) as exc_info:
            list(source.records())
        assert exc_info.value.line_no == 2

    def test_missing_t_receipt_raises_source_error(self, tmp_path: Path) -> None:
        p = self._write_gz(tmp_path, ['{"game_id": "x"}'])
        source = LocalFileSource(p, "live_pbp")
        with pytest.raises(SourceError):
            list(source.records())

    def test_missing_key_field_raises_source_error(self, tmp_path: Path) -> None:
        # trade channel needs frame.msg.market_ticker
        p = self._write_gz(tmp_path, ['{"t_receipt": 1.0, "frame": {"msg": {}}}'])
        source = LocalFileSource(p, "trade")
        with pytest.raises(SourceError):
            list(source.records())

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        p = self._write_gz(
            tmp_path, ['{"t_receipt": 1.0, "game_id": "g1", "frame": {}}', "", "   "]
        )
        source = LocalFileSource(p, "live_pbp")
        records = list(source.records())
        assert len(records) == 1
        assert records[0] == SourceRecord(
            t_receipt_ns=1_000_000_000,
            topic="nba.game_state",
            key="g1",
            raw=b'{"t_receipt": 1.0, "game_id": "g1", "frame": {}}',
        )
