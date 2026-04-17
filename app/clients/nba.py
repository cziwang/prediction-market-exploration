"""Python client for the NBA Prediction Market API.

Usage:
    from app.clients.nba import NBAClient

    client = NBAClient()                          # default: http://127.0.0.1:8000
    client = NBAClient("http://localhost:9000")    # custom base URL

    markets = client.list_markets(status="finalized", limit=10)
    market  = client.get_market("KXNBAGAME-26APR14MIACHA-MIA")
    candles = client.get_candlesticks("KXNBAGAME-26APR14MIACHA-MIA")
    summary = client.summary()
"""

from __future__ import annotations

from typing import Any

import requests


class NBAClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # -- Markets --

    def list_markets(
        self,
        *,
        status: str | None = None,
        result: str | None = None,
        event_ticker: str | None = None,
        search: str | None = None,
        sort: str = "close_time",
        order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sort": sort, "order": order, "limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if result:
            params["result"] = result
        if event_ticker:
            params["event_ticker"] = event_ticker
        if search:
            params["search"] = search
        return self._get("/markets", params)

    def get_market(self, ticker: str) -> dict[str, Any]:
        return self._get(f"/markets/{ticker}")

    # -- Events --

    def list_events(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return self._get("/events", {"limit": limit, "offset": offset})

    def get_event(self, event_ticker: str) -> dict[str, Any]:
        return self._get(f"/events/{event_ticker}")

    # -- Candlesticks --

    def get_candlesticks(
        self,
        ticker: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if start_ts is not None:
            params["start_ts"] = start_ts
        if end_ts is not None:
            params["end_ts"] = end_ts
        return self._get(f"/markets/{ticker}/candlesticks", params)

    # -- Analytics --

    def summary(self) -> dict[str, Any]:
        return self._get("/analytics/summary")

    def biggest_swings(self, *, limit: int = 20) -> dict[str, Any]:
        return self._get("/analytics/biggest-swings", {"limit": limit})

    def volume_leaders(self, *, limit: int = 20) -> dict[str, Any]:
        return self._get("/analytics/volume-leaders", {"limit": limit})

    def result_distribution(self) -> dict[str, Any]:
        return self._get("/analytics/result-distribution")
