"""Kalshi WebSocket client for orderbook data collection."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import websockets

from .auth import build_ws_headers
from .book import BookManager, L2Book

log = logging.getLogger(__name__)

DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"
PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


@dataclass
class WSConfig:
    url: str = DEMO_WS_URL
    tickers: list[str] = field(default_factory=list)
    resnapshot_interval_s: float = 10.0
    reconnect_delay_s: float = 2.0
    max_reconnect_delay_s: float = 60.0


class KalshiWSClient:
    """Connects to Kalshi WS, subscribes to orderbook channels, maintains L2 books."""

    def __init__(self, config: WSConfig, book_mgr: BookManager | None = None):
        self.config = config
        self.book_mgr = book_mgr or BookManager()
        self._ws = None
        self._running = False
        self._cmd_id = 0
        self._sub_ids: dict[int, str] = {}  # sid -> ticker

    # -- Public API ------------------------------------------------------

    async def run(self) -> None:
        """Connect and process messages forever, reconnecting on failure."""
        self._running = True
        delay = self.config.reconnect_delay_s

        while self._running:
            try:
                await self._connect_and_listen()
                delay = self.config.reconnect_delay_s  # reset on clean disconnect
            except (
                websockets.ConnectionClosed,
                websockets.InvalidStatusCode,
                OSError,
            ) as exc:
                log.warning("WS disconnected: %s — reconnecting in %.1fs", exc, delay)
                self.book_mgr.invalidate_all()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.max_reconnect_delay_s)
            except asyncio.CancelledError:
                break

    def stop(self) -> None:
        self._running = False

    # -- Connection lifecycle --------------------------------------------

    async def _connect_and_listen(self) -> None:
        headers = build_ws_headers()
        async with websockets.connect(
            self.config.url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            log.info("Connected to %s", self.config.url)

            await self._subscribe_all()

            # Run message loop + periodic re-snapshot concurrently.
            listen_task = asyncio.create_task(self._listen(ws))
            resnapshot_task = asyncio.create_task(self._resnapshot_loop(ws))

            try:
                await asyncio.gather(listen_task, resnapshot_task)
            finally:
                listen_task.cancel()
                resnapshot_task.cancel()

    async def _subscribe_all(self) -> None:
        """Subscribe to orderbook_delta for all configured tickers."""
        for ticker in self.config.tickers:
            self._cmd_id += 1
            cmd = {
                "id": self._cmd_id,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_tickers": [ticker],
                },
            }
            await self._ws.send(json.dumps(cmd))
            log.info("Subscribed to orderbook_delta for %s", ticker)

    async def _listen(self, ws) -> None:
        """Process incoming WS messages."""
        async for raw in ws:
            frame = json.loads(raw)
            msg_type = frame.get("type")

            if msg_type in ("orderbook_snapshot", "orderbook_delta"):
                book = self.book_mgr.process_frame(frame)
                if book is not None:
                    self._on_book_update(book, frame)
                elif msg_type == "orderbook_delta" and book is None:
                    # Book was invalidated (crossed) or missing — request re-snapshot.
                    ticker = frame.get("msg", {}).get("market_ticker")
                    if ticker:
                        log.warning("Book invalidated for %s, requesting re-snapshot", ticker)
                        await self._request_snapshot(ws, ticker)

            elif msg_type == "trade":
                self._on_trade(frame)

            elif msg_type == "subscribed":
                sid = frame.get("sid")
                # Map subscription IDs for tracking.
                log.info("Subscription confirmed: sid=%s", sid)

            elif msg_type == "error":
                log.error("WS error: %s", frame)

    async def _resnapshot_loop(self, ws) -> None:
        """Periodically request fresh snapshots to correct drift."""
        while True:
            await asyncio.sleep(self.config.resnapshot_interval_s)
            for ticker in self.config.tickers:
                await self._request_snapshot(ws, ticker)

    async def _request_snapshot(self, ws, ticker: str) -> None:
        self._cmd_id += 1
        cmd = {
            "id": self._cmd_id,
            "cmd": "update_subscription",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": [ticker],
                "action": "get_snapshot",
            },
        }
        await ws.send(json.dumps(cmd))

    # -- Callbacks (override in subclass or replace) ---------------------

    def _on_book_update(self, book: L2Book, frame: dict) -> None:
        """Called after every book-modifying message."""
        bid = book.best_yes_bid
        ask = book.best_yes_ask
        spread = book.spread
        log.debug(
            "%s  bid=%s ask=%s spread=%s",
            book.market_ticker,
            bid,
            ask,
            spread,
        )

    def _on_trade(self, frame: dict) -> None:
        """Called on trade messages."""
        msg = frame.get("msg", {})
        log.debug(
            "TRADE %s  %s %s @ %s",
            msg.get("market_ticker"),
            msg.get("taker_side"),
            msg.get("count_fp"),
            msg.get("yes_price_dollars"),
        )
