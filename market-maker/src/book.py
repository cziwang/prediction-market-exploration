"""L2 order book maintained from Kalshi WebSocket snapshots and deltas.

Kalshi binary contracts have YES and NO sides. The book stores bids on each
side as price_cents -> size mappings. The implied YES ask is derived from the
NO bid side: best_yes_ask = 100 - best_no_bid.

All prices are integer cents [1, 99]. All sizes are integer contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self


def dollars_to_cents(price_str: str) -> int:
    """Convert a Kalshi 4-decimal dollar string to integer cents.

    >>> dollars_to_cents("0.5500")
    55
    """
    return int(round(float(price_str) * 100))


def parse_size(size_str: str) -> int:
    """Convert a Kalshi fixed-point size string to integer contracts.

    >>> parse_size("1000.00")
    1000
    """
    return int(round(float(size_str)))


@dataclass
class L2Book:
    """Level-2 order book for a single Kalshi market.

    Attributes:
        market_ticker: Kalshi market ticker.
        yes_bids: price_cents -> size for YES side.
        no_bids: price_cents -> size for NO side.
        seq: last processed sequence number.
        sid: subscription ID for stale-message filtering.
    """

    market_ticker: str
    yes_bids: dict[int, int] = field(default_factory=dict)
    no_bids: dict[int, int] = field(default_factory=dict)
    seq: int | None = None
    sid: int | None = None

    # -- Snapshot --------------------------------------------------------

    @classmethod
    def from_snapshot(cls, msg: dict, sid: int, seq: int) -> Self:
        """Build a book from an ``orderbook_snapshot`` message payload."""
        yes = {}
        for price_str, size_str in msg.get("yes_dollars_fp", []):
            p, s = dollars_to_cents(price_str), parse_size(size_str)
            if s > 0:
                yes[p] = s

        no = {}
        for price_str, size_str in msg.get("no_dollars_fp", []):
            p, s = dollars_to_cents(price_str), parse_size(size_str)
            if s > 0:
                no[p] = s

        return cls(
            market_ticker=msg["market_ticker"],
            yes_bids=yes,
            no_bids=no,
            seq=seq,
            sid=sid,
        )

    # -- Delta application -----------------------------------------------

    def apply_delta(self, price_cents: int, delta: int, side: str) -> None:
        """Apply a single delta to the book.

        ``delta`` can be negative (fills reduce resting size).
        If the resulting size is <= 0 the price level is removed.
        """
        target = self.yes_bids if side == "yes" else self.no_bids
        new_size = target.get(price_cents, 0) + delta
        if new_size <= 0:
            target.pop(price_cents, None)
        else:
            target[price_cents] = new_size

    # -- BBO helpers -----------------------------------------------------

    @property
    def best_yes_bid(self) -> int | None:
        """Highest YES bid price in cents."""
        return max(self.yes_bids) if self.yes_bids else None

    @property
    def best_yes_ask(self) -> int | None:
        """Implied YES ask = 100 - highest NO bid."""
        if not self.no_bids:
            return None
        return 100 - max(self.no_bids)

    @property
    def spread(self) -> int | None:
        """Spread in cents between best YES ask and best YES bid."""
        bid, ask = self.best_yes_bid, self.best_yes_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    @property
    def midpoint(self) -> float | None:
        """Simple midpoint of the YES side BBO."""
        bid, ask = self.best_yes_bid, self.best_yes_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @property
    def is_crossed(self) -> bool:
        """True if best bid >= best ask (book is corrupted)."""
        s = self.spread
        return s is not None and s <= 0

    # -- Depth -----------------------------------------------------------

    def yes_bid_levels(self, depth: int = 10) -> list[tuple[int, int]]:
        """Top ``depth`` YES bid levels, best first (descending price)."""
        return sorted(self.yes_bids.items(), reverse=True)[:depth]

    def yes_ask_levels(self, depth: int = 10) -> list[tuple[int, int]]:
        """Top ``depth`` implied YES ask levels, best first (ascending price).

        Each NO bid at price P implies a YES ask at 100-P with the same size.
        """
        levels = [(100 - p, s) for p, s in self.no_bids.items()]
        return sorted(levels)[:depth]

    def __repr__(self) -> str:
        bid = self.best_yes_bid
        ask = self.best_yes_ask
        bid_s = f"{bid}c" if bid is not None else "---"
        ask_s = f"{ask}c" if ask is not None else "---"
        return f"L2Book({self.market_ticker} {bid_s}/{ask_s})"


@dataclass
class BookManager:
    """Manages L2 books for multiple markets, routing WS frames."""

    books: dict[str, L2Book] = field(default_factory=dict)

    def process_frame(self, frame: dict) -> L2Book | None:
        """Process a raw WS JSON frame and return the affected book, or None.

        Handles ``orderbook_snapshot`` and ``orderbook_delta`` message types.
        Trade messages do NOT modify the book (the fill is already reflected
        in the corresponding negative delta).

        Returns None if the frame is stale or unrecognized.
        """
        msg_type = frame.get("type")
        sid = frame.get("sid")
        seq = frame.get("seq")
        msg = frame.get("msg", {})

        if msg_type == "orderbook_snapshot":
            book = L2Book.from_snapshot(msg, sid=sid, seq=seq)
            self.books[book.market_ticker] = book
            return book

        if msg_type == "orderbook_delta":
            ticker = msg.get("market_ticker")
            if ticker is None:
                return None

            book = self.books.get(ticker)
            if book is None:
                # Delta without a snapshot — can't apply.
                return None

            # Drop stale messages from old subscriptions.
            if book.sid is not None and sid != book.sid:
                return None

            price_cents = dollars_to_cents(msg["price_dollars"])
            delta = parse_size(msg["delta_fp"])
            side = msg["side"]

            book.apply_delta(price_cents, delta, side)
            book.seq = seq

            # Invalidate crossed books.
            if book.is_crossed:
                del self.books[ticker]
                return None

            return book

        # trade / other message types — no book update.
        return None

    def get(self, ticker: str) -> L2Book | None:
        return self.books.get(ticker)

    def invalidate(self, ticker: str) -> None:
        """Remove a book (e.g. on connection change)."""
        self.books.pop(ticker, None)

    def invalidate_all(self) -> None:
        """Remove all books (e.g. on reconnect)."""
        self.books.clear()
