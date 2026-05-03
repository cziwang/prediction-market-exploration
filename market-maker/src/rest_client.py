"""Kalshi REST API client for orderbook snapshots and market discovery."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .auth import build_rest_headers
from .book import dollars_to_cents, parse_size

log = logging.getLogger(__name__)

DEMO_REST_URL = "https://demo-api.kalshi.co"
PROD_REST_URL = "https://api.elections.kalshi.com"


@dataclass
class RestConfig:
    base_url: str = DEMO_REST_URL
    timeout_s: float = 10.0


class KalshiRestClient:
    """Authenticated REST client for Kalshi API."""

    def __init__(self, config: RestConfig | None = None):
        self.config = config or RestConfig()
        self._client = httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout_s,
        )

    def _authed_get(self, path: str, params: dict | None = None) -> dict:
        headers = build_rest_headers("GET", path)
        resp = self._client.get(path, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    # -- Market discovery ------------------------------------------------

    def get_active_markets(
        self,
        status: str = "open",
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Fetch active markets, paginating through the cursor.

        Passing *series_ticker* or *event_ticker* filters server-side,
        which is far cheaper than fetching everything.
        """
        all_markets = []
        cursor: str | None = None

        while True:
            params: dict = {"status": status, "limit": limit}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if cursor:
                params["cursor"] = cursor

            data = self._authed_get("/trade-api/v2/markets", params=params)
            markets = data.get("markets", [])
            all_markets.extend(markets)

            cursor = data.get("cursor")
            if not cursor or not markets:
                break

        log.info(
            "Discovered %d active markets (series=%s, event=%s)",
            len(all_markets),
            series_ticker or "*",
            event_ticker or "*",
        )
        return all_markets

    def get_active_tickers(
        self,
        status: str = "open",
        series_tickers: list[str] | None = None,
        event_ticker: str | None = None,
    ) -> list[str]:
        """Return tickers for active markets, optionally filtered.

        *series_tickers* accepts a list — issues one paginated query per
        series and merges results.  This keeps each API call small.
        """
        if series_tickers:
            tickers: list[str] = []
            for st in series_tickers:
                markets = self.get_active_markets(
                    status=status, series_ticker=st,
                )
                tickers.extend(m["ticker"] for m in markets)
            return tickers

        markets = self.get_active_markets(
            status=status, event_ticker=event_ticker,
        )
        return [m["ticker"] for m in markets]

    # -- Orderbook snapshots ---------------------------------------------

    def get_orderbook(self, ticker: str) -> dict:
        """Fetch the full orderbook for a single market.

        Returns raw API response with ``orderbook`` containing
        ``yes`` and ``no`` arrays of [price, quantity] pairs.
        """
        path = f"/trade-api/v2/markets/{ticker}/orderbook"
        data = self._authed_get(path)
        return data

    def get_orderbook_parsed(self, ticker: str) -> dict:
        """Fetch orderbook and parse into cents/integer format.

        Returns::

            {
                "market_ticker": ticker,
                "ts": <epoch_ms>,
                "levels": [
                    {"side": "yes", "price_cents": 55, "qty": 100},
                    ...
                ]
            }
        """
        ts_ms = int(time.time() * 1000)
        raw = self.get_orderbook(ticker)
        ob = raw.get("orderbook", {})

        levels = []
        for price_str, qty_str in ob.get("yes", []):
            levels.append({
                "side": "yes",
                "price_cents": dollars_to_cents(price_str),
                "qty": parse_size(qty_str),
            })
        for price_str, qty_str in ob.get("no", []):
            levels.append({
                "side": "no",
                "price_cents": dollars_to_cents(price_str),
                "qty": parse_size(qty_str),
            })

        return {
            "market_ticker": ticker,
            "ts": ts_ms,
            "levels": levels,
        }

    def get_orderbooks_batch(
        self,
        tickers: list[str],
        delay_s: float = 0.05,
    ) -> list[dict]:
        """Fetch orderbooks for multiple tickers with rate-limit pacing."""
        results = []
        for i, ticker in enumerate(tickers):
            try:
                parsed = self.get_orderbook_parsed(ticker)
                results.append(parsed)
            except httpx.HTTPStatusError as exc:
                log.warning("Failed to fetch orderbook for %s: %s", ticker, exc)
            except httpx.TimeoutException:
                log.warning("Timeout fetching orderbook for %s", ticker)

            if delay_s > 0 and i < len(tickers) - 1:
                time.sleep(delay_s)

        log.info("Fetched %d/%d orderbook snapshots", len(results), len(tickers))
        return results

    def close(self) -> None:
        self._client.close()
