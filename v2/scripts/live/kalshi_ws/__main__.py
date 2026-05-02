"""Live Kalshi WS ingester → bronze + silver (v2 format).

Data-only ingester — no strategy, no trading. Connects to Kalshi's
WebSocket, subscribes to orderbook_delta + trade for all NBA series,
and writes:

  Bronze: raw frames as gzip-JSONL (authoritative archive)
  Silver: typed events as Parquet v=3 (explicit schemas, int64 ns
          timestamps, dictionary encoding, sorted by t_receipt_ns)

Usage:
    python -m v2.scripts.live.kalshi_ws
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import time
import uuid
from pathlib import Path

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

from v2.app.clients.kalshi_sdk import make_client, paginate_markets
from v2.app.services.bronze_writer import BronzeWriter
from v2.app.services.silver_writer import SilverWriter
from v2.app.transforms.kalshi_ws import KalshiTransform

load_dotenv()

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"

SERIES_TICKERS = [
    "KXNBAGAME",    # win/loss
    "KXNBASPREAD",  # point spread
    "KXNBATOTAL",   # total points over/under
    "KXNBAPTS",     # player points
]
PUBLIC_CHANNELS = ["orderbook_delta", "trade"]

RECONNECT_INITIAL = 1.0
RECONNECT_MAX = 60.0
NO_MARKETS_BACKOFF = 300.0

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


def _fetch_open_tickers_by_series() -> dict[str, list[str]]:
    """Map of series → list of currently-open market tickers via REST."""
    api = make_client()
    return {
        series: [
            m.ticker
            for m in paginate_markets(api, series_ticker=series, status="open")
        ]
        for series in SERIES_TICKERS
    }


class Ingester:
    def __init__(self, bronze: BronzeWriter, silver: SilverWriter) -> None:
        self.bronze = bronze
        self.silver = silver
        self._transform = KalshiTransform()
        self._shutdown = asyncio.Event()
        self._ws: websockets.ClientConnection | None = None
        self._conn_id: str | None = None

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
        self._conn_id = uuid.uuid4().hex
        by_series = await asyncio.to_thread(_fetch_open_tickers_by_series)
        total = sum(len(t) for t in by_series.values())
        if total == 0:
            log.warning("no open markets across %s — idling", SERIES_TICKERS)
            return False
        log.info(
            "subscribing %s to %d markets across %d series (%s)",
            PUBLIC_CHANNELS,
            total,
            sum(1 for t in by_series.values() if t),
            ", ".join(f"{s}={len(t)}" for s, t in by_series.items()),
        )

        headers = _build_auth_headers()
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=WS_PING_INTERVAL,
            ping_timeout=WS_PING_TIMEOUT,
        ) as ws:
            self._ws = ws
            try:
                msg_id = 1
                for series, tickers in by_series.items():
                    if not tickers:
                        continue
                    await ws.send(json.dumps({
                        "id": msg_id,
                        "cmd": "subscribe",
                        "params": {
                            "channels": PUBLIC_CHANNELS,
                            "market_tickers": tickers,
                        },
                    }))
                    msg_id += 1

                async for raw in ws:
                    if self._shutdown.is_set():
                        break
                    await self._handle_frame(raw)
            finally:
                self._ws = None
        return True

    async def _handle_frame(self, raw: str | bytes) -> None:
        t_receipt = time.time()
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("non-json frame: %r", raw[:200] if isinstance(raw, (str, bytes)) else raw)
            return

        # Bronze: archive raw frame
        channel = frame.get("type") or "unknown"
        await self.bronze.emit(
            {
                "source": "kalshi_ws",
                "channel": channel,
                "t_receipt": t_receipt,
                "conn_id": self._conn_id,
                "frame": frame,
            },
            channel=channel,
        )

        # Transform: raw frame → typed events
        try:
            events = self._transform(frame, t_receipt, conn_id=self._conn_id)
        except Exception:
            log.exception("transform error on %s", channel)
            return

        # Silver: write typed events (v=3 format)
        for event in events:
            await self.silver.emit(event)

    def shutdown(self) -> None:
        log.info("shutdown requested")
        self._shutdown.set()
        ws = self._ws
        if ws is not None:
            asyncio.create_task(ws.close())


async def _main() -> None:
    async with BronzeWriter(source="kalshi_ws") as bronze, \
               SilverWriter(source="kalshi_ws", flush_seconds=60) as silver:
        ingester = Ingester(bronze, silver)

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
