"""Normalization result types (DESIGN.md D8).

Normalizers never raise on bad input and never silently drop records:
every input maps to either Ok(event) or Dlq(raw, error, context).
"""

from dataclasses import dataclass

from pm.core.events import MarketEvent


@dataclass(frozen=True)
class Ok:
    event: MarketEvent


@dataclass(frozen=True)
class Dlq:
    """Dead-letter record: the raw input plus why it could not be normalized."""

    raw: bytes
    error: str
    context: str  # e.g. source topic / file / channel


NormalizeResult = Ok | Dlq
