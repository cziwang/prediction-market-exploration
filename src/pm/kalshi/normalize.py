"""Normalize bronze Kalshi WS envelopes into typed events.

Bronze envelope shape (one JSON object per line):

    {"source": "kalshi_ws", "channel": "trade" | "orderbook_delta",
     "t_receipt": <float seconds>, "frame": {"type": ..., "sid": N, "seq": N,
     "msg": {...}}}

Wire-format conversions happen here and nowhere else:
- "0.1500" dollars           -> 15 cents           (round(float * 100))
- "2.58" fractional contracts -> 258 centi-contracts (round(float * 100))
"""

import json
from typing import Any

from pm.core.dlq import Dlq, NormalizeResult, Ok
from pm.kalshi.events import BookUpdateEvent, TradeEvent


def _dollars_to_cents(dollars: str | float) -> int:
    return round(float(dollars) * 100)


def _fp_to_centi(fp: str | float) -> int:
    return round(float(fp) * 100)


def _receipt_ns(t_receipt: float) -> int:
    return int(t_receipt * 1_000_000_000)


def normalize_kalshi(raw: bytes, context: str = "kalshi_ws") -> NormalizeResult:
    """Normalize one bronze Kalshi WS line. Never raises; bad input -> Dlq."""
    try:
        record: dict[str, Any] = json.loads(raw)
        channel = record["channel"]
        frame = record["frame"]
        msg = frame["msg"]
        t_receipt_ns = _receipt_ns(record["t_receipt"])

        if channel == "trade":
            taker_side = msg["taker_side"]
            if taker_side not in ("yes", "no"):
                return Dlq(raw, f"invalid taker_side: {taker_side!r}", context)
            return Ok(
                TradeEvent(
                    t_receipt_ns=t_receipt_ns,
                    source="kalshi_ws",
                    market_ticker=msg["market_ticker"],
                    price_cents=_dollars_to_cents(msg["yes_price_dollars"]),
                    size_cc=_fp_to_centi(msg["count_fp"]),
                    taker_side=taker_side,
                )
            )

        if channel == "orderbook_delta":
            side = msg["side"]
            if side not in ("yes", "no"):
                return Dlq(raw, f"invalid side: {side!r}", context)
            return Ok(
                BookUpdateEvent(
                    t_receipt_ns=t_receipt_ns,
                    source="kalshi_ws",
                    market_ticker=msg["market_ticker"],
                    side=side,
                    price_cents=_dollars_to_cents(msg["price_dollars"]),
                    delta_cc=_fp_to_centi(msg["delta_fp"]),
                    seq=int(frame["seq"]),
                    sid=int(frame["sid"]),
                )
            )

        return Dlq(raw, f"unknown channel: {channel!r}", context)

    except Exception as exc:  # noqa: BLE001 — boundary: any bad input becomes Dlq
        return Dlq(raw, f"{type(exc).__name__}: {exc}", context)
