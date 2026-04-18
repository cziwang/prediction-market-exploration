"""Live Kalshi WS ingester → bronze S3.

Connects to Kalshi's WebSocket, subscribes to `orderbook_delta` for every
currently-open KXNBAGAME market, and archives each raw frame to bronze
via BronzeWriter. No transform, no silver — just archival.

Kalshi requires an authenticated connection even for public market data,
so the handshake signs `timestamp + "GET" + path` with RSA-PSS using the
same credentials as the REST client (`KALSHI_API_KEY_ID`,
`KALSHI_PRIVATE_KEY_PATH`).

Channels written to bronze are keyed by the server-provided message `type`
(`orderbook_snapshot`, `orderbook_delta`, `ok`, `error`, ...), so the
snapshot/delta split falls out naturally and control messages land under
their own tiny prefixes.

Usage:
    python -m scripts.live.kalshi_ws
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import time
from pathlib import Path

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

from app.clients.kalshi_sdk import make_client, paginate_markets
from app.services.bronze_writer import BronzeWriter

load_dotenv()

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"

SERIES_TICKER = "KXNBAGAME"
CHANNELS = ["orderbook_delta"]

RECONNECT_INITIAL = 1.0
RECONNECT_MAX = 60.0
NO_MARKETS_BACKOFF = 300.0  # 5 min when KXNBAGAME has nothing open

WS_PING_INTERVAL = 30
WS_PING_TIMEOUT = 10

log = logging.getLogger("live.kalshi_ws")


def _sign(key_path: Path, timestamp_ms: str, method: str, path: str) -> str:
    with open(key_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
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


def _build_auth_headers() -> dict[str, str]:
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path_str = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path_str:
        raise RuntimeError(
            "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set "
            "(Kalshi requires auth even for public WS channels)"
        )
    key_path = Path(key_path_str)
    if not key_path.exists():
        raise RuntimeError(f"Kalshi private key not found at {key_path}")
    timestamp_ms = str(int(time.time() * 1000))
    signature = _sign(key_path, timestamp_ms, "GET", WS_SIGN_PATH)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


def _fetch_open_tickers() -> list[str]:
    """All currently-open KXNBAGAME market tickers via REST."""
    api = make_client()
    return [
        m.ticker
        for m in paginate_markets(api, series_ticker=SERIES_TICKER, status="open")
    ]


class Ingester:
    def __init__(self, bronze: BronzeWriter) -> None:
        self.bronze = bronze
        self._shutdown = asyncio.Event()
        self._ws: websockets.ClientConnection | None = None

    async def run(self) -> None:
        backoff = RECONNECT_INITIAL
        while not self._shutdown.is_set():
            sleep_s = backoff
            try:
                connected = await self._connect_once()
                if self._shutdown.is_set():
                    break
                if connected:
                    backoff = RECONNECT_INITIAL
                else:
                    sleep_s = NO_MARKETS_BACKOFF
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ws session failed")
                backoff = min(backoff * 2, RECONNECT_MAX)

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_s)
                break
            except asyncio.TimeoutError:
                pass

    async def _connect_once(self) -> bool:
        tickers = await asyncio.to_thread(_fetch_open_tickers)
        if not tickers:
            log.warning("no open %s markets — idling", SERIES_TICKER)
            return False
        log.info("subscribing %s to %d markets", CHANNELS, len(tickers))

        headers = _build_auth_headers()
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=WS_PING_INTERVAL,
            ping_timeout=WS_PING_TIMEOUT,
        ) as ws:
            self._ws = ws
            try:
                await ws.send(json.dumps({
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": CHANNELS,
                        "market_tickers": tickers,
                    },
                }))
                async for raw in ws:
                    if self._shutdown.is_set():
                        break
                    await self._archive(raw)
            finally:
                self._ws = None
        return True

    async def _archive(self, raw: str | bytes) -> None:
        t_receipt = time.time()
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("non-json frame: %r", raw[:200] if isinstance(raw, (str, bytes)) else raw)
            return
        channel = frame.get("type") or "unknown"
        await self.bronze.emit(
            {
                "source": "kalshi_ws",
                "channel": channel,
                "t_receipt": t_receipt,
                "frame": frame,
            },
            channel=channel,
        )

    def shutdown(self) -> None:
        log.info("shutdown requested")
        self._shutdown.set()
        ws = self._ws
        if ws is not None:
            asyncio.create_task(ws.close())


async def _main() -> None:
    async with BronzeWriter(source="kalshi_ws") as bronze:
        ingester = Ingester(bronze)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, ingester.shutdown)
        await ingester.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main())


if __name__ == "__main__":
    main()
