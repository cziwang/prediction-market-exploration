"""Kalshi API client with SDK for live data and raw HTTP for historical.

If KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH are set, the client
authenticates via RSA-PSS signatures (handled by the SDK).

The kalshi-python SDK does not wrap /historical/* endpoints, so this
module provides raw HTTP helpers for those.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import requests
from dotenv import load_dotenv
from kalshi_python import KalshiClient as _SDKClient
from kalshi_python.api.markets_api import MarketsApi
from kalshi_python.configuration import Configuration

load_dotenv()

HOST = "https://api.elections.kalshi.com/trade-api/v2"


# ---------------------------------------------------------------------------
# SDK client (live endpoints)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Raw HTTP helpers (historical endpoints not in SDK)
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict:
    """GET a JSON endpoint from the Kalshi API."""
    resp = requests.get(f"{HOST}{path}", params=params)
    resp.raise_for_status()
    return resp.json()


def get_historical_cutoff() -> dict:
    """Get cutoff timestamps defining the live/historical boundary."""
    return _get("/historical/cutoff")


def get_historical_markets(
    *, series_ticker: str | None = None, limit: int = 1000, cursor: str | None = None,
) -> dict:
    """Fetch settled markets from the historical database."""
    params: dict[str, Any] = {"limit": limit}
    if series_ticker:
        params["series_ticker"] = series_ticker
    if cursor:
        params["cursor"] = cursor
    return _get("/historical/markets", params)


def paginate_historical_markets(
    *, series_ticker: str | None = None,
) -> Iterator[dict]:
    """Yield all historical markets, handling cursor pagination."""
    cursor = None
    while True:
        resp = get_historical_markets(series_ticker=series_ticker, cursor=cursor)
        for m in resp.get("markets") or []:
            yield m
        cursor = resp.get("cursor")
        if not cursor:
            return


def get_historical_trades(
    *, ticker: str | None = None, min_ts: int | None = None,
    max_ts: int | None = None, limit: int = 1000, cursor: str | None = None,
) -> dict:
    """Fetch trades from the historical database."""
    params: dict[str, Any] = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    if min_ts:
        params["min_ts"] = min_ts
    if max_ts:
        params["max_ts"] = max_ts
    if cursor:
        params["cursor"] = cursor
    return _get("/historical/trades", params)


def paginate_historical_trades(
    *, ticker: str | None = None, min_ts: int | None = None,
    max_ts: int | None = None,
) -> Iterator[dict]:
    """Yield all historical trades for a market, handling cursor pagination."""
    cursor = None
    while True:
        resp = get_historical_trades(
            ticker=ticker, min_ts=min_ts, max_ts=max_ts, cursor=cursor,
        )
        for t in resp.get("trades") or []:
            yield t
        cursor = resp.get("cursor")
        if not cursor:
            return


def get_historical_candlesticks(
    ticker: str, *, start_ts: int, end_ts: int, period_interval: int = 60,
) -> dict:
    """Fetch OHLC candlestick data for a historical market."""
    return _get(f"/historical/markets/{ticker}/candlesticks", {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": period_interval,
    })
