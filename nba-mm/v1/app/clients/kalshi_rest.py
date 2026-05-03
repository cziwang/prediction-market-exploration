"""Kalshi historical API client using raw HTTP.

The kalshi-python SDK does not wrap /historical/* endpoints, so this
module provides raw HTTP helpers with cursor pagination and retry on 429.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import requests

HOST = "https://api.elections.kalshi.com/trade-api/v2"


def _get(path: str, params: dict | None = None, _retries: int = 3) -> dict:
    """GET a JSON endpoint from the Kalshi API, with retry on 429."""
    for attempt in range(_retries):
        resp = requests.get(f"{HOST}{path}", params=params)
        if resp.status_code == 429:
            wait = 2 ** (attempt + 1)
            print(f"  rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
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
