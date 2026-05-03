"""Kalshi API authentication via RSA-PSS signing."""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def load_private_key(path: str | Path):
    """Load an RSA private key from PEM file."""
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """Sign ``timestamp_ms + method + path`` with RSA-PSS SHA256."""
    msg = (timestamp_ms + method + path).encode()
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def build_ws_headers(
    key_id: str | None = None,
    key_path: str | None = None,
    ws_path: str = "/trade-api/ws/v2",
) -> dict[str, str]:
    """Build authentication headers for the Kalshi WebSocket connection."""
    key_id = key_id or os.environ["KALSHI_API_KEY_ID"]
    key_path = key_path or os.environ["KALSHI_PRIVATE_KEY_PATH"]

    private_key = load_private_key(key_path)
    timestamp_ms = str(int(time.time() * 1000))
    signature = sign_request(private_key, timestamp_ms, "GET", ws_path)

    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


def build_rest_headers(
    method: str,
    path: str,
    key_id: str | None = None,
    key_path: str | None = None,
) -> dict[str, str]:
    """Build authentication headers for a Kalshi REST API request."""
    key_id = key_id or os.environ["KALSHI_API_KEY_ID"]
    key_path = key_path or os.environ["KALSHI_PRIVATE_KEY_PATH"]

    private_key = load_private_key(key_path)
    timestamp_ms = str(int(time.time() * 1000))
    signature = sign_request(private_key, timestamp_ms, method, path)

    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }
