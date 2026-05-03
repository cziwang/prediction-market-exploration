"""Shared conversion utilities for Kalshi data.

Used by book state, transforms, and replay engine.
"""

from __future__ import annotations

from datetime import datetime


def dollars_to_cents(s: str) -> int:
    """Convert Kalshi 4-decimal dollar string to integer cents.

    '0.5200' → 52, '0.01' → 1.
    """
    return int(round(float(s) * 100))


def parse_ts(ts) -> float | None:
    """Parse Kalshi timestamp to float seconds.

    Handles: epoch int/float (seconds or ms), epoch string, ISO string, None.
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return ts / 1000.0 if ts > 1e12 else float(ts)
    if isinstance(ts, str):
        if not ts:
            return None
        try:
            val = float(ts)
            return val / 1000.0 if val > 1e12 else val
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, AttributeError):
            pass
    return None
