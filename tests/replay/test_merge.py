"""Tests for pm.replay.merge."""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pm.replay.merge import OrderingError, merge_sources
from pm.replay.sources import LocalFileSource, SourceRecord

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden"


class ListSource:
    """In-memory BronzeSource for unit tests."""

    def __init__(self, timestamps: list[int], source_id: str = "test") -> None:
        self._timestamps = timestamps
        self._source_id = source_id

    @property
    def source_id(self) -> str:
        return self._source_id

    def records(self):
        for ts in self._timestamps:
            yield SourceRecord(t_receipt_ns=ts, topic="t", key="k", raw=b"{}")


def merged_timestamps(*sources: ListSource) -> list[int]:
    return [r.t_receipt_ns for r in merge_sources(list(sources))]


class TestMerge:
    def test_two_sorted_sources_interleave(self) -> None:
        a = ListSource([1, 3, 7])
        b = ListSource([2, 5, 6])
        assert merged_timestamps(a, b) == [1, 2, 3, 5, 6, 7]

    def test_single_source_passthrough(self) -> None:
        assert merged_timestamps(ListSource([1, 2, 3])) == [1, 2, 3]

    def test_empty_source_ignored(self) -> None:
        assert merged_timestamps(ListSource([]), ListSource([4, 5])) == [4, 5]

    def test_all_empty(self) -> None:
        assert merged_timestamps(ListSource([]), ListSource([])) == []

    def test_no_sources(self) -> None:
        assert list(merge_sources([])) == []

    def test_duplicate_timestamps_allowed(self) -> None:
        # Equal timestamps are valid (non-decreasing, not strictly increasing)
        a = ListSource([1, 1, 2])
        b = ListSource([1, 2])
        assert merged_timestamps(a, b) == [1, 1, 1, 2, 2]

    def test_tie_break_is_deterministic(self) -> None:
        a = ListSource([5], source_id="a")
        b = ListSource([5], source_id="b")
        run1 = merged_timestamps(a, b)
        a2 = ListSource([5], source_id="a")
        b2 = ListSource([5], source_id="b")
        run2 = merged_timestamps(a2, b2)
        assert run1 == run2 == [5, 5]

    def test_out_of_order_source_raises(self) -> None:
        bad = ListSource([3, 1], source_id="bad_file")
        with pytest.raises(OrderingError) as exc_info:
            list(merge_sources([bad]))
        assert exc_info.value.source_id == "bad_file"
        assert exc_info.value.prev_ns == 3
        assert exc_info.value.current_ns == 1

    def test_out_of_order_detected_mid_merge(self) -> None:
        good = ListSource(list(range(100)))
        bad = ListSource([50, 49], source_id="bad")
        with pytest.raises(OrderingError):
            list(merge_sources([good, bad]))

    @given(
        st.lists(
            st.lists(st.integers(min_value=0, max_value=10**15)).map(sorted),
            min_size=0,
            max_size=8,
        )
    )
    def test_property_merge_of_sorted_sources_is_sorted(
        self, source_lists: list[list[int]]
    ) -> None:
        sources = [ListSource(ts) for ts in source_lists]
        merged = [r.t_receipt_ns for r in merge_sources(sources)]
        assert merged == sorted(merged)
        assert len(merged) == sum(len(ts) for ts in source_lists)


class TestGoldenMerge:
    def test_golden_pbp_and_trades_merge_sorted(self) -> None:
        sources = [
            LocalFileSource(GOLDEN / "nba_pbp_0042500121.jsonl.gz", "live_pbp"),
            LocalFileSource(GOLDEN / "kalshi_trades_0042500121.jsonl.gz", "trade"),
        ]
        merged = list(merge_sources(sources))
        assert len(merged) == 548 + 20376
        timestamps = [r.t_receipt_ns for r in merged]
        assert timestamps == sorted(timestamps)

    def test_golden_merge_interleaves_topics(self) -> None:
        # Trades cover 00:15-00:51 UTC; PBP covers the whole game. In the
        # overlap window the merged stream must alternate between topics
        # rather than emitting one topic as a block.
        sources = [
            LocalFileSource(GOLDEN / "nba_pbp_0042500121.jsonl.gz", "live_pbp"),
            LocalFileSource(GOLDEN / "kalshi_trades_0042500121.jsonl.gz", "trade"),
        ]
        merged = list(merge_sources(sources))
        # Find the window where both topics are active
        first_trade_idx = next(
            i for i, r in enumerate(merged) if r.topic == "kalshi.trades"
        )
        window = merged[first_trade_idx:]
        topics_in_window = {r.topic for r in window}
        assert topics_in_window == {"kalshi.trades", "nba.game_state"}
