"""Kalshi REST order client for live market making (Phase 2).

Places and cancels limit orders via Kalshi's REST API. Order lifecycle
notifications (ACKs, fills) arrive separately via WS push channels —
this client is fire-and-forget for placements.

Includes:
- RSA-PSS authentication (same as WS handshake)
- Circuit breaker (opens after N consecutive failures)
- Auth retry on 401
- Rate limit retry on 429 with backoff

Uses requests + asyncio.to_thread to match the existing codebase pattern
(no aiohttp dependency). The order queue drainer calls these methods from
a single asyncio task, so thread-safety is not a concern.

Does NOT deliver ACKs or fills — those come from WS user_orders/fill
channels and are dispatched by the ingester.
"""

from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

HOST = "https://api.elections.kalshi.com/trade-api/v2"
SIGN_PREFIX = "/trade-api/v2"  # Signature must include the full path from root
MAX_ORDER_SIZE = 10  # hard safety limit

log = logging.getLogger(__name__)


@dataclass
class OpenOrder:
    """Lightweight representation of a resting order from Kalshi."""
    order_id: str
    client_order_id: str | None
    ticker: str
    side: str        # "yes" or "no"
    action: str      # "buy" or "sell"
    price_cents: int
    remaining: int   # contracts still resting


def _fp_to_int(fp_str: str) -> int:
    """Convert Kalshi fixed-point string like '10.00' to int."""
    return int(round(float(fp_str)))


def _dollars_to_cents(dollar_str: str) -> int:
    """Convert Kalshi dollar string like '0.4500' to cents."""
    return int(round(float(dollar_str) * 100))


class KalshiOrderClient:
    """Place and cancel limit orders on Kalshi via REST.

    All public methods are async (using asyncio.to_thread internally)
    so they integrate with the async ingester loop.
    """

    def __init__(
        self,
        circuit_breaker_threshold: int = 3,
    ) -> None:
        self._key_id = os.environ["KALSHI_API_KEY_ID"]
        self._key_path = Path(os.environ["KALSHI_PRIVATE_KEY_PATH"])
        if not self._key_path.exists():
            raise RuntimeError(
                f"Kalshi private key not found at {self._key_path}"
            )
        self._key = self._load_key()
        self._session = requests.Session()

        self._circuit_breaker_threshold = circuit_breaker_threshold
        self._consecutive_failures: int = 0
        self.circuit_open: bool = False

    def _load_key(self) -> Any:
        with open(self._key_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        msg = (timestamp_ms + method + path).encode()
        sig = self._key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        sign_path = SIGN_PREFIX + path
        signature = self._sign(timestamp_ms, method, sign_path)
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
        }

    def _request_sync(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
        retries: int = 2,
    ) -> dict:
        """Synchronous authenticated request with retry on 401 and 429."""
        url = f"{HOST}{path}"

        for attempt in range(retries + 1):
            headers = self._auth_headers(method, path)
            try:
                resp = self._session.request(
                    method, url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=10,
                )

                if resp.status_code == 401 and attempt < retries:
                    log.warning("401 on %s %s — refreshing auth", method, path)
                    self._key = self._load_key()
                    continue

                if resp.status_code == 429 and attempt < retries:
                    wait = 2 ** (attempt + 1)
                    log.warning("429 on %s %s — waiting %ds", method, path, wait)
                    time.sleep(wait)
                    continue

                body = resp.json()
                if resp.status_code >= 400:
                    error_msg = body.get("error", body)
                    raise KalshiAPIError(resp.status_code, error_msg)

                # Success — reset circuit breaker
                self._consecutive_failures = 0
                self.circuit_open = False
                return body

            except KalshiAPIError:
                self._record_failure()
                raise
            except Exception as e:
                self._record_failure()
                if attempt < retries:
                    log.warning("request error on %s %s: %s — retrying",
                                method, path, e)
                    continue
                raise

        raise RuntimeError(f"exhausted retries for {method} {path}")

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Async wrapper around _request_sync via to_thread."""
        return await asyncio.to_thread(
            self._request_sync, method, path, json_body, params,
        )

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._circuit_breaker_threshold:
            if not self.circuit_open:
                log.error(
                    "CIRCUIT BREAKER OPEN after %d consecutive failures",
                    self._consecutive_failures,
                )
            self.circuit_open = True

    # -----------------------------------------------------------------
    # Order placement
    # -----------------------------------------------------------------

    async def place_limit(
        self,
        ticker: str,
        side: str,
        price_cents: int,
        size: int,
        client_order_id: str | None = None,
    ) -> None:
        """Fire-and-forget limit order placement.

        Args:
            ticker: Market ticker (e.g. "KXNBAPTS-26APR26CLETOR-...")
            side: "bid" or "ask" (mapped to Kalshi's action/side fields)
            price_cents: YES price in cents (1-99)
            size: Number of contracts (1 to MAX_ORDER_SIZE)
            client_order_id: Caller-generated UUID for WS ACK correlation

        The confirmed order_id arrives via the user_orders WS channel,
        NOT from this method's return value.
        """
        if size > MAX_ORDER_SIZE:
            raise ValueError(
                f"order size {size} exceeds hard limit {MAX_ORDER_SIZE}"
            )
        if not 1 <= price_cents <= 99:
            raise ValueError(f"price_cents {price_cents} out of range 1-99")

        # Map internal bid/ask to Kalshi's API:
        #   bid = buy YES at price_cents
        #   ask = sell YES at price_cents
        action = "buy" if side == "bid" else "sell"

        body: dict[str, Any] = {
            "ticker": ticker,
            "side": "yes",
            "action": action,
            "type": "limit",
            "yes_price": price_cents,
            "count": size,
            "post_only": True,
            "time_in_force": "good_till_canceled",
        }
        if client_order_id:
            body["client_order_id"] = client_order_id

        log.info(
            "placing %s YES@%dc x%d on %s (client_order_id=%s)",
            action, price_cents, size, ticker,
            client_order_id or "none",
        )
        await self._request("POST", "/portfolio/orders", json_body=body)

    # -----------------------------------------------------------------
    # Order cancellation
    # -----------------------------------------------------------------

    async def cancel(self, order_id: str) -> None:
        """Cancel a single order by Kalshi order_id."""
        log.info("cancelling order %s", order_id)
        await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def cancel_all(self, ticker: str) -> None:
        """Cancel all resting orders on a ticker.

        Fetches resting orders for the ticker, then batch-cancels them.
        """
        orders = await self.get_open_orders(ticker=ticker)
        if not orders:
            return

        batch = [{"order_id": o.order_id} for o in orders]
        log.info("batch cancelling %d orders on %s", len(batch), ticker)
        await self._request(
            "DELETE", "/portfolio/orders/batched",
            json_body={"orders": batch},
        )

    # -----------------------------------------------------------------
    # Query endpoints (for bootstrap and reconciliation)
    # -----------------------------------------------------------------

    async def get_positions(self) -> dict[str, int]:
        """Fetch current positions from Kalshi. Returns ticker -> net contracts.

        Used at startup (bootstrap) and as a fallback reconciliation source.
        """
        positions: dict[str, int] = {}
        cursor = None
        while True:
            params: dict[str, Any] = {
                "limit": 1000,
                "count_filter": "position",
            }
            if cursor:
                params["cursor"] = cursor
            resp = await self._request(
                "GET", "/portfolio/positions", params=params,
            )
            for mp in resp.get("market_positions") or []:
                pos = _fp_to_int(mp["position_fp"])
                if pos != 0:
                    positions[mp["ticker"]] = pos
            cursor = resp.get("cursor")
            if not cursor:
                break
        return positions

    async def get_open_orders(
        self, ticker: str | None = None,
    ) -> list[OpenOrder]:
        """Fetch all resting orders. Optionally filtered by ticker."""
        orders: list[OpenOrder] = []
        cursor = None
        while True:
            params: dict[str, Any] = {
                "limit": 1000,
                "status": "resting",
            }
            if ticker:
                params["ticker"] = ticker
            if cursor:
                params["cursor"] = cursor
            resp = await self._request(
                "GET", "/portfolio/orders", params=params,
            )
            for o in resp.get("orders") or []:
                remaining = _fp_to_int(o["remaining_count_fp"])
                if remaining > 0:
                    orders.append(OpenOrder(
                        order_id=o["order_id"],
                        client_order_id=o.get("client_order_id"),
                        ticker=o["ticker"],
                        side=o["side"],
                        action=o["action"],
                        price_cents=_dollars_to_cents(o["yes_price_dollars"]),
                        remaining=remaining,
                    ))
            cursor = resp.get("cursor")
            if not cursor:
                break
        return orders

    # -----------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------

    def close(self) -> None:
        self._session.close()


class KalshiAPIError(Exception):
    """Raised when Kalshi returns an HTTP error."""

    def __init__(self, status: int, detail: Any) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Kalshi API {status}: {detail}")
