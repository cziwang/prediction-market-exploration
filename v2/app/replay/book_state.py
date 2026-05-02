"""Full-depth order book state for one ticker.

Maintains yes and no books as dict[int, int] (price_cents → size).
Supports delta application, snapshot seeding, BBO derivation, full-depth
level queries, and invariant validation.

Unlike the BBO-only OrderBookState in transforms/kalshi_ws.py, this class
preserves all depth levels for backtesting and analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BookValidationError:
    """A single book invariant violation."""
    check: str
    detail: str


class ReplayBookState:
    """Full-depth order book for one ticker.

    Attributes:
        yes_book: price_cents → resting size for YES side (bids).
        no_book: price_cents → resting size for NO side.
        seq: last processed sequence number (for gap detection).
        sid: subscription ID this book belongs to.
    """

    __slots__ = ("yes_book", "no_book", "seq", "sid")

    def __init__(self) -> None:
        self.yes_book: dict[int, int] = {}
        self.no_book: dict[int, int] = {}
        self.seq: int | None = None
        self.sid: int | None = None

    @classmethod
    def from_snapshot(cls, msg: dict) -> ReplayBookState:
        """Seed book from an orderbook_snapshot with price levels.

        If the snapshot is an empty marker (no yes_dollars_fp/no_dollars_fp),
        returns a book with empty dicts — subsequent deltas will build it up.
        """
        book = cls()
        for price_str, size_str in msg.get("yes_dollars_fp", []):
            p = _dollars_to_cents(price_str)
            s = int(round(float(size_str)))
            if s > 0:
                book.yes_book[p] = s
        for price_str, size_str in msg.get("no_dollars_fp", []):
            p = _dollars_to_cents(price_str)
            s = int(round(float(size_str)))
            if s > 0:
                book.no_book[p] = s
        return book

    def apply_delta(self, price_cents: int, delta: int, side: str) -> None:
        """Apply a single size change at one price level.

        Args:
            price_cents: price level (1-99)
            delta: size change (positive = added, negative = removed)
            side: "yes" or "no"
        """
        target = self.yes_book if side == "yes" else self.no_book
        new_size = target.get(price_cents, 0) + delta
        if new_size <= 0:
            target.pop(price_cents, None)
        else:
            target[price_cents] = new_size

    # --- BBO ---

    @property
    def best_bid(self) -> int | None:
        """Highest YES price with resting orders."""
        return max(self.yes_book) if self.yes_book else None

    @property
    def best_ask(self) -> int | None:
        """Cheapest price to buy YES = 100 - highest NO price."""
        if not self.no_book:
            return None
        return 100 - max(self.no_book)

    @property
    def bid_size(self) -> int:
        """Size at best bid."""
        if not self.yes_book:
            return 0
        return self.yes_book[max(self.yes_book)]

    @property
    def ask_size(self) -> int:
        """Size at best ask."""
        if not self.no_book:
            return 0
        return self.no_book[max(self.no_book)]

    @property
    def spread(self) -> int | None:
        """Ask - bid in cents. None if either side is empty."""
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return a - b

    @property
    def mid(self) -> int | None:
        """Midpoint in cents. None if either side is empty."""
        b, a = self.best_bid, self.best_ask
        if b is None or a is None:
            return None
        return (b + a) // 2

    def bbo(self) -> tuple[int | None, int | None, int, int]:
        """Return (best_bid, best_ask, bid_size, ask_size)."""
        return (self.best_bid, self.best_ask, self.bid_size, self.ask_size)

    # --- Full depth ---

    def levels(self, side: str) -> list[tuple[int, int]]:
        """Sorted price levels for one side.

        Args:
            side: "yes" or "no"

        Returns:
            List of (price_cents, size) sorted by price ascending.
        """
        target = self.yes_book if side == "yes" else self.no_book
        return sorted(target.items())

    def to_snapshot(self) -> dict:
        """Full book state as a dict for serialization.

        Returns:
            {
                "yes_levels": [(price, size), ...],
                "no_levels": [(price, size), ...],
                "best_bid": int | None,
                "best_ask": int | None,
                "bid_size": int,
                "ask_size": int,
                "spread": int | None,
                "seq": int | None,
            }
        """
        return {
            "yes_levels": self.levels("yes"),
            "no_levels": self.levels("no"),
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "spread": self.spread,
            "seq": self.seq,
        }

    # --- Validation ---

    def validate(self) -> list[BookValidationError]:
        """Check book invariants. Returns list of violations (empty = healthy)."""
        errors: list[BookValidationError] = []

        # Negative sizes
        for price, size in self.yes_book.items():
            if size < 0:
                errors.append(BookValidationError(
                    "negative_size",
                    f"yes book price={price} size={size}",
                ))
        for price, size in self.no_book.items():
            if size < 0:
                errors.append(BookValidationError(
                    "negative_size",
                    f"no book price={price} size={size}",
                ))

        # Price range (1-99 cents)
        for price in self.yes_book:
            if price < 1 or price > 99:
                errors.append(BookValidationError(
                    "price_out_of_range",
                    f"yes book price={price}",
                ))
        for price in self.no_book:
            if price < 1 or price > 99:
                errors.append(BookValidationError(
                    "price_out_of_range",
                    f"no book price={price}",
                ))

        # Crossed book
        b, a = self.best_bid, self.best_ask
        if b is not None and a is not None and b >= a:
            errors.append(BookValidationError(
                "crossed_book",
                f"best_bid={b} >= best_ask={a}",
            ))

        return errors


def _dollars_to_cents(s: str) -> int:
    """'0.5200' → 52."""
    return int(round(float(s) * 100))
