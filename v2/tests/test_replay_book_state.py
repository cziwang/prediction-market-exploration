"""Tests for ReplayBookState."""

from v2.app.replay.book_state import ReplayBookState


class TestFromSnapshot:
    def test_empty_snapshot(self):
        """Empty marker snapshot (settled market) creates empty book."""
        msg = {"market_ticker": "KXNBA-TEST", "market_id": "abc"}
        book = ReplayBookState.from_snapshot(msg)
        assert book.yes_book == {}
        assert book.no_book == {}
        assert book.best_bid is None
        assert book.best_ask is None

    def test_full_snapshot(self):
        """Snapshot with price levels seeds the book correctly."""
        msg = {
            "market_ticker": "KXNBA-TEST",
            "yes_dollars_fp": [["0.5500", "9863.00"], ["0.5000", "5000.00"]],
            "no_dollars_fp": [["0.4400", "40844.00"], ["0.4000", "1000.00"]],
        }
        book = ReplayBookState.from_snapshot(msg)
        assert book.yes_book == {55: 9863, 50: 5000}
        assert book.no_book == {44: 40844, 40: 1000}
        assert book.best_bid == 55
        assert book.best_ask == 56  # 100 - 44
        assert book.bid_size == 9863
        assert book.ask_size == 40844

    def test_zero_size_levels_excluded(self):
        """Levels with zero size are not added to the book."""
        msg = {
            "yes_dollars_fp": [["0.5500", "0.00"], ["0.5000", "100.00"]],
            "no_dollars_fp": [],
        }
        book = ReplayBookState.from_snapshot(msg)
        assert 55 not in book.yes_book
        assert book.yes_book == {50: 100}


class TestApplyDelta:
    def test_add_to_empty_book(self):
        book = ReplayBookState()
        book.apply_delta(55, 1000, "yes")
        assert book.yes_book == {55: 1000}
        assert book.best_bid == 55

    def test_add_to_existing_level(self):
        book = ReplayBookState()
        book.apply_delta(55, 1000, "yes")
        book.apply_delta(55, 500, "yes")
        assert book.yes_book[55] == 1500

    def test_remove_partial(self):
        book = ReplayBookState()
        book.apply_delta(55, 1000, "yes")
        book.apply_delta(55, -200, "yes")
        assert book.yes_book[55] == 800

    def test_remove_full_clears_level(self):
        book = ReplayBookState()
        book.apply_delta(55, 1000, "yes")
        book.apply_delta(55, -1000, "yes")
        assert 55 not in book.yes_book

    def test_remove_beyond_zero_clears_level(self):
        book = ReplayBookState()
        book.apply_delta(55, 100, "yes")
        book.apply_delta(55, -200, "yes")
        assert 55 not in book.yes_book

    def test_no_side(self):
        book = ReplayBookState()
        book.apply_delta(44, 5000, "no")
        assert book.no_book == {44: 5000}
        assert book.best_ask == 56  # 100 - 44

    def test_cancel_and_replace(self):
        """Simulates moving a bid from 19c to 17c."""
        book = ReplayBookState()
        book.apply_delta(19, 10000, "yes")
        assert book.best_bid == 19

        book.apply_delta(19, -10000, "yes")
        book.apply_delta(17, 10000, "yes")
        assert book.best_bid == 17
        assert 19 not in book.yes_book


class TestBBO:
    def test_empty_book(self):
        book = ReplayBookState()
        assert book.bbo() == (None, None, 0, 0)
        assert book.spread is None
        assert book.mid is None

    def test_one_side_only(self):
        book = ReplayBookState()
        book.apply_delta(55, 100, "yes")
        assert book.bbo() == (55, None, 100, 0)
        assert book.spread is None

    def test_both_sides(self):
        book = ReplayBookState()
        book.apply_delta(55, 100, "yes")
        book.apply_delta(44, 200, "no")
        assert book.bbo() == (55, 56, 100, 200)
        assert book.spread == 1
        assert book.mid == 55  # (55 + 56) // 2


class TestLevels:
    def test_sorted_ascending(self):
        book = ReplayBookState()
        book.apply_delta(55, 100, "yes")
        book.apply_delta(50, 200, "yes")
        book.apply_delta(60, 50, "yes")
        assert book.levels("yes") == [(50, 200), (55, 100), (60, 50)]

    def test_empty_side(self):
        book = ReplayBookState()
        assert book.levels("yes") == []
        assert book.levels("no") == []


class TestSnapshot:
    def test_roundtrip(self):
        book = ReplayBookState()
        book.apply_delta(55, 100, "yes")
        book.apply_delta(50, 200, "yes")
        book.apply_delta(44, 300, "no")
        book.seq = 12345

        snap = book.to_snapshot()
        assert snap["yes_levels"] == [(50, 200), (55, 100)]
        assert snap["no_levels"] == [(44, 300)]
        assert snap["best_bid"] == 55
        assert snap["best_ask"] == 56
        assert snap["spread"] == 1
        assert snap["seq"] == 12345


class TestValidation:
    def test_healthy_book(self):
        book = ReplayBookState()
        book.apply_delta(55, 100, "yes")
        book.apply_delta(44, 200, "no")
        assert book.validate() == []

    def test_empty_book_is_valid(self):
        book = ReplayBookState()
        assert book.validate() == []

    def test_crossed_book(self):
        book = ReplayBookState()
        book.yes_book[57] = 100  # bid at 57
        book.no_book[44] = 200   # ask at 56
        # Not crossed: 57 >= 56
        # Wait — best_ask = 100 - 44 = 56, best_bid = 57 → crossed
        errors = book.validate()
        assert len(errors) == 1
        assert errors[0].check == "crossed_book"

    def test_negative_size(self):
        book = ReplayBookState()
        book.yes_book[55] = -100  # shouldn't happen, but test detection
        errors = book.validate()
        assert len(errors) == 1
        assert errors[0].check == "negative_size"

    def test_price_out_of_range(self):
        book = ReplayBookState()
        book.yes_book[0] = 100
        book.yes_book[100] = 200
        errors = book.validate()
        assert len(errors) == 2
        assert all(e.check == "price_out_of_range" for e in errors)
