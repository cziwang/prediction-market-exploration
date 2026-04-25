"""REST trading actions for the dashboard.

Thin wrapper around Kalshi REST API for manual order management.
All functions are synchronous (called on button click in Streamlit).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

REST_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _sign(key_path: Path, timestamp_ms: str, method: str, path: str) -> str:
    with open(key_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    import base64
    msg = (timestamp_ms + method + path).encode()
    sig = key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def _auth_headers(method: str, path: str) -> dict[str, str]:
    key_id = os.environ["KALSHI_API_KEY_ID"]
    key_path = Path(os.environ["KALSHI_PRIVATE_KEY_PATH"])
    timestamp_ms = str(int(time.time() * 1000))
    signature = _sign(key_path, timestamp_ms, method, path)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "Content-Type": "application/json",
    }


def place_order(
    ticker: str, side: str, action: str, price_cents: int, size: int,
) -> dict:
    """Place a limit order. Returns the API response dict.

    side: "yes" or "no"
    action: "buy" or "sell"
    price_cents: 1-99
    """
    path = "/trade-api/v2/portfolio/orders"
    headers = _auth_headers("POST", path)
    body = {
        "ticker": ticker,
        "action": action,
        "side": side,
        "type": "limit",
        "yes_price": price_cents,
        "count": size,
        "client_order_id": str(uuid.uuid4()),
    }
    resp = requests.post(f"{REST_URL}/portfolio/orders", json=body, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def cancel_order(order_id: str) -> dict:
    """Cancel a single order by ID."""
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = _auth_headers("DELETE", path)
    resp = requests.delete(f"{REST_URL}/portfolio/orders/{order_id}", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def cancel_all_orders() -> dict:
    """Cancel all resting orders across all markets."""
    path = "/trade-api/v2/portfolio/orders"
    headers = _auth_headers("DELETE", path)
    resp = requests.delete(f"{REST_URL}/portfolio/orders", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def flatten_position(ticker: str, position: int, best_bid: int, best_ask: int) -> dict | None:
    """Place an aggressive order to close a position.

    If long: sell YES at the bid (cross the spread to guarantee fill).
    If short: buy YES at the ask.
    """
    if position == 0:
        return None
    if position > 0:
        return place_order(ticker, "yes", "sell", best_bid, abs(position))
    else:
        return place_order(ticker, "yes", "buy", best_ask, abs(position))
