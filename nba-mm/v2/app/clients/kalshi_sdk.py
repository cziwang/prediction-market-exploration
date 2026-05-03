"""Kalshi live API client using the official kalshi-python SDK.

Copied from v1 — identical interface. Used by the live ingester to
discover open markets for WS subscription.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv
from kalshi_python import KalshiClient as _SDKClient
from kalshi_python.api.markets_api import MarketsApi
from kalshi_python.configuration import Configuration

load_dotenv()

HOST = "https://api.elections.kalshi.com/trade-api/v2"


def make_client() -> MarketsApi:
    """Create a configured MarketsApi instance with optional auth."""
    config = Configuration(host=HOST)
    client = _SDKClient(configuration=config)

    key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if key_id and key_path and Path(key_path).exists():
        client.set_kalshi_auth(key_id, key_path)

    return MarketsApi(client)


def paginate_markets(
    api: MarketsApi, **kwargs: Any
) -> Iterator[Any]:
    """Yield Market objects from a cursor-paginated get_markets call."""
    cursor = None
    while True:
        resp = api.get_markets(cursor=cursor, **kwargs)
        for m in resp.markets or []:
            yield m
        cursor = resp.cursor
        if not cursor:
            return
