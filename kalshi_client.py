"""Thin Kalshi API client with optional RSA-PSS signed auth.

Public endpoints (markets, events, historical, candlesticks) work unauthenticated.
If KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH are set, every request is signed
per https://docs.kalshi.com/getting_started/quick_start_authenticated_requests.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Iterator

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PATH_PREFIX = "/trade-api/v2"


class KalshiClient:
    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.base_url = base_url
        self.key_id = key_id or os.getenv("KALSHI_API_KEY_ID") or None
        key_path = private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH")

        self._private_key: rsa.RSAPrivateKey | None = None
        if self.key_id and key_path and Path(key_path).exists():
            with open(key_path, "rb") as f:
                loaded = serialization.load_pem_private_key(f.read(), password=None)
            if not isinstance(loaded, rsa.RSAPrivateKey):
                raise ValueError(f"Expected RSA private key, got {type(loaded)}")
            self._private_key = loaded

        self.session = requests.Session()

    @property
    def authenticated(self) -> bool:
        return self._private_key is not None

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        assert self._private_key is not None
        message = (timestamp_ms + method + path).encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _headers(self, method: str, path: str) -> dict[str, str]:
        if not self.authenticated:
            return {}
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id or "",
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path),
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET {path}. `path` is relative to /trade-api/v2, e.g. '/markets'."""
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        signed_path = PATH_PREFIX + path  # signature uses path without query string

        last_exc: Exception | None = None
        for attempt in range(6):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=self._headers("GET", signed_path),
                    timeout=30,
                )
            except requests.RequestException as e:
                last_exc = e
                time.sleep(min(2**attempt, 30))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                sleep_s = float(retry_after) if retry_after else min(2**attempt, 30)
                time.sleep(sleep_s)
                continue
            resp.raise_for_status()
            return resp.json()
        if last_exc:
            raise last_exc
        resp.raise_for_status()
        return {}

    def paginate(
        self, path: str, params: dict[str, Any], items_key: str
    ) -> Iterator[dict[str, Any]]:
        """Yield items from a cursor-paginated Kalshi endpoint."""
        params = dict(params)
        while True:
            data = self.get(path, params)
            for item in data.get(items_key, []):
                yield item
            cursor = data.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor
