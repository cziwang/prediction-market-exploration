"""Tests for BookState and extract_depth_row."""

from v2.app.core.book_state import BookState, BookValidationError, extract_depth_row


# ── BookState tests (migrated from test_replay_book_state.py) ──


class TestFromSnapshot:
    def test_empty_snapshot(self):
        msg = {"market_ticker": "KXNBA-TEST", "market_id": "abc"}
        book = BookState.from_snapshot(msg)
        assert book.yes_book == {}
        assert book.no_book == {}
        assert book.best_bid is None
        assert book.best_ask is None

    def test_full_snapshot(self):
        msg = {
            "market_ticker": "KXNBA-TEST",
            "yes_dollars_fp": [["0.5500", "9863.00"], ["0.5000", "5000.00"]],
            "no_dollars_fp": [["0.4400", "40844.00"], ["0.4000", "1000.00"]],
        }
        book = BookState.from_snapshot(msg)
        assert book.yes_book == {55: 9863, 50: 5000}
        assert book.no_book == {44: 40844, 40: 1000}
        assert book.best_bid == 55
        assert book.best_ask == 56  # 100 - 44
        assert book.bid_size == 9863
        assert book.ask_size == 40844

    def test_zero_size_levels_excluded(self):
        msg = {
            "yes_dollars_fp": [["0.5500", "0.00"], ["0.5000", "100.00"]],
            "no_dollars_fp": [],
        }
        book = BookState.from_snapshot(msg)
        assert 55 not in book.yes_book
        assert book.yes_book == {50: 100}


class TestApplyDelta:
    def test_add_to_empty_book(self):
        book = BookState()
        book.apply_delta(55, 1000, "yes")
        assert book.yes_book == {55: 1000}
        assert book.best_bid == 55

    def test_add_to_existing_level(self):
        book = BookState()
        book.apply_delta(55, 1000, "yes")
        book.apply_delta(55, 500, "yes")
        assert book.yes_book[55] == 1500

    def test_remove_partial(self):
        book = BookState()
        book.apply_delta(55, 1000, "yes")
        book.apply_delta(55, -200, "yes")
        assert book.yes_book[55] == 800

    def test_remove_full_clears_level(self):
        book = BookState()
        book.apply_delta(55, 1000, "yes")
        book.apply_delta(55, -1000, "yes")
        assert 55 not in book.yes_book

    def test_remove_beyond_zero_clears_level(self):
        book = BookState()
        book.apply_delta(55, 100, "yes")
        book.apply_delta(55, -200, "yes")
        assert 55 not in book.yes_book

    def test_no_side(self):
        book = BookState()
        book.apply_delta(44, 5000, "no")
        assert book.no_book == {44: 5000}
        assert book.best_ask == 56  # 100 - 44

    def test_cancel_and_replace(self):
        book = BookState()
        book.apply_delta(19, 10000, "yes")
        assert book.best_bid == 19

        book.apply_delta(19, -10000, "yes")
        book.apply_delta(17, 10000, "yes")
        assert book.best_bid == 17
        assert 19 not in book.yes_book


class TestBBO:
    def test_empty_book(self):
        book = BookState()
        assert book.bbo() == (None, None, 0, 0)
        assert book.spread is None
        assert book.mid is None

    def test_one_side_only(self):
        book = BookState()
        book.apply_delta(55, 100, "yes")
        assert book.bbo() == (55, None, 100, 0)
        assert book.spread is None

    def test_both_sides(self):
        book = BookState()
        book.apply_delta(55, 100, "yes")
        book.apply_delta(44, 200, "no")
        assert book.bbo() == (55, 56, 100, 200)
        assert book.spread == 1
        assert book.mid == 55


class TestLevels:
    def test_sorted_ascending(self):
        book = BookState()
        book.apply_delta(55, 100, "yes")
        book.apply_delta(50, 200, "yes")
        book.apply_delta(60, 50, "yes")
        assert book.levels("yes") == [(50, 200), (55, 100), (60, 50)]

    def test_empty_side(self):
        book = BookState()
        assert book.levels("yes") == []
        assert book.levels("no") == []


class TestSnapshot:
    def test_roundtrip(self):
        book = BookState()
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
        book = BookState()
        book.apply_delta(55, 100, "yes")
        book.apply_delta(44, 200, "no")
        assert book.validate() == []

    def test_empty_book_is_valid(self):
        book = BookState()
        assert book.validate() == []

    def test_crossed_book(self):
        book = BookState()
        book.yes_book[57] = 100
        book.no_book[44] = 200
        errors = book.validate()
        assert len(errors) == 1
        assert errors[0].check == "crossed_book"

    def test_negative_size(self):
        book = BookState()
        book.yes_book[55] = -100
        errors = book.validate()
        assert len(errors) == 1
        assert errors[0].check == "negative_size"

    def test_price_out_of_range(self):
        book = BookState()
        book.yes_book[0] = 100
        book.yes_book[100] = 200
        errors = book.validate()
        assert len(errors) == 2
        assert all(e.check == "price_out_of_range" for e in errors)


# ── extract_depth_row tests ──


def _make_book(**kwargs) -> BookState:
    """Helper: build a book with yes/no levels."""
    book = BookState()
    for price, size in kwargs.get("yes", []):
        book.apply_delta(price, size, "yes")
    for price, size in kwargs.get("no", []):
        book.apply_delta(price, size, "no")
    book.seq = kwargs.get("seq", 1)
    book.sid = kwargs.get("sid", 1)
    return book


class TestExtractDepthRow:
    def test_empty_book(self):
        book = BookState()
        row = extract_depth_row(book, 1000, None, "TEST", 1, 1)
        assert row["bid_1"] is None
        assert row["ask_1"] is None
        assert row["spread"] is None
        assert row["mid_x2"] is None
        assert row["bid_depth_5c"] == 0
        assert row["num_bid_levels"] == 0

    def test_single_level_each_side(self):
        book = _make_book(yes=[(55, 1000)], no=[(44, 2000)])
        row = extract_depth_row(book, 1000, 900, "TEST", 1, 1)
        assert row["bid_1"] == 55
        assert row["bid_1_size"] == 1000
        assert row["ask_1"] == 56  # 100 - 44
        assert row["ask_1_size"] == 2000
        assert row["bid_2"] is None
        assert row["ask_2"] is None
        assert row["spread"] == 1
        assert row["mid_x2"] == 111  # 55 + 56

    def test_bid_levels_sorted_descending(self):
        book = _make_book(yes=[(50, 100), (55, 200), (53, 300)])
        row = extract_depth_row(book, 1000, None, "TEST", 1, 1)
        assert row["bid_1"] == 55  # best (highest)
        assert row["bid_2"] == 53
        assert row["bid_3"] == 50  # worst

    def test_ask_levels_sorted_ascending(self):
        # NO book: 44 → ask 56, 40 → ask 60, 42 → ask 58
        book = _make_book(no=[(44, 100), (40, 200), (42, 300)])
        row = extract_depth_row(book, 1000, None, "TEST", 1, 1)
        assert row["ask_1"] == 56  # best (cheapest), from no=44
        assert row["ask_2"] == 58  # from no=42
        assert row["ask_3"] == 60  # from no=40

    def test_depth_within_5c(self):
        book = _make_book(
            yes=[(55, 1000), (54, 500), (53, 300), (50, 2000), (40, 100)],
        )
        row = extract_depth_row(book, 1000, None, "TEST", 1, 1)
        # bid_depth_5c: levels >= 55-5=50 → 55+54+53+50 = 1000+500+300+2000 = 3800
        assert row["bid_depth_5c"] == 3800
        # bid_depth_10c: levels >= 55-10=45 → same + 40 is excluded (40 < 45)
        assert row["bid_depth_10c"] == 3800

    def test_depth_within_10c(self):
        book = _make_book(
            yes=[(55, 1000), (50, 500), (45, 300)],
        )
        row = extract_depth_row(book, 1000, None, "TEST", 1, 1)
        assert row["bid_depth_10c"] == 1800  # all three: >= 55-10=45

    def test_num_levels_counts_all(self):
        # 15 yes levels — more than DEPTH_LEVELS (10)
        levels = [(i, 100) for i in range(30, 45)]
        book = _make_book(yes=levels)
        row = extract_depth_row(book, 1000, None, "TEST", 1, 1)
        assert row["num_bid_levels"] == 15  # counts all, not just top 10
        assert row["bid_10"] is not None
        # Level 11+ not in row (capped at 10)

    def test_identity_fields(self):
        row = extract_depth_row(
            BookState(), 123456789, 987654321, "TICKER-X", 42, 7,
        )
        assert row["t_receipt_ns"] == 123456789
        assert row["t_exchange_ns"] == 987654321
        assert row["market_ticker"] == "TICKER-X"
        assert row["seq"] == 42
        assert row["sid"] == 7

    def test_column_count(self):
        book = _make_book(yes=[(55, 1000)], no=[(44, 2000)])
        row = extract_depth_row(book, 1000, None, "TEST", 1, 1)
        assert len(row) == 53
