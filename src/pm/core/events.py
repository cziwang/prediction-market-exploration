"""Base event type shared by all sources (DESIGN.md §2).

All events are immutable and carry an explicit schema_version (D8).
Units — integers only, no floats for money or quantity:
- Prices: integer cents ("0.1500" dollars -> 15).
- Sizes:  integer centi-contracts ("2.58" contracts -> 258, D9).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class MarketEvent(BaseModel):
    """Base for all normalized events."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    t_receipt_ns: int  # when our system received this record
    source: Literal["kalshi_ws", "nba_cdn"]
