"""Orchestrates live data collection: WS deltas/trades + REST snapshots → S3 bronze."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import websockets

from .auth import build_ws_headers
from .book import BookManager, L2Book, dollars_to_cents, parse_size
from .parquet_writer import BronzeWriter, WriterConfig
from .rest_client import KalshiRestClient, RestConfig

log = logging.getLogger(__name__)

DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"
PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


@dataclass
class CollectorConfig:
    ws_url: str = DEMO_WS_URL
    rest_base_url: str = "https://demo-api.kalshi.co"
    tickers: list[str] = field(default_factory=list)
    auto_discover: bool = False
    series_tickers: list[str] = field(default_factory=list)
    snapshot_interval_s: float = 900.0  # 15 min REST snapshot cycle
    ws_resnapshot_s: float = 10.0
    reconnect_delay_s: float = 2.0
    max_reconnect_delay_s: float = 60.0
    writer_config: WriterConfig = field(default_factory=WriterConfig)


class Collector:
    """Runs WS + REST collection, writing all data to S3 bronze."""

    def __init__(self, config: CollectorConfig):
        self.config = config
        self.book_mgr = BookManager()
        self.writer = BronzeWriter(config.writer_config)
        self.rest = KalshiRestClient(RestConfig(base_url=config.rest_base_url))
        self._ws = None
        self._running = False
        self._cmd_id = 0
        self._tickers: list[str] = list(config.tickers)

    # -- Entry point -----------------------------------------------------

    async def run(self) -> None:
        """Start collection: WS listener + REST snapshot poller."""
        self._running = True

        if self.config.auto_discover:
            series = self.config.series_tickers or None
            log.info("Discovering active markets (series=%s)...", series or "ALL")
            self._tickers = await asyncio.to_thread(
                self.rest.get_active_tickers,
                series_tickers=series,
            )
            log.info("Discovered %d tickers", len(self._tickers))

        if not self._tickers:
            log.error("No tickers to collect. Exiting.")
            return

        # Run WS and REST poller concurrently.
        ws_task = asyncio.create_task(self._ws_loop())
        rest_task = asyncio.create_task(self._rest_snapshot_loop())
        flush_task = asyncio.create_task(self._flush_loop())

        try:
            await asyncio.gather(ws_task, rest_task, flush_task)
        except asyncio.CancelledError:
            pass
        finally:
            self.writer.flush_all()
            self.rest.close()

    def stop(self) -> None:
        self._running = False

    # -- WS loop ---------------------------------------------------------

    async def _ws_loop(self) -> None:
        delay = self.config.reconnect_delay_s
        while self._running:
            try:
                await self._ws_connect_and_listen()
                delay = self.config.reconnect_delay_s
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

    async def _ws_connect_and_listen(self) -> None:
        headers = build_ws_headers()
        async with websockets.connect(
            self.config.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            log.info("WS connected to %s", self.config.ws_url)

            # Subscribe in batches to avoid overwhelming the connection.
            batch_size = 50
            for i in range(0, len(self._tickers), batch_size):
                batch = self._tickers[i : i + batch_size]
                self._cmd_id += 1
                cmd = {
                    "id": self._cmd_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta", "trade"],
                        "market_tickers": batch,
                    },
                }
                await ws.send(json.dumps(cmd))

            log.info("Subscribed to %d tickers", len(self._tickers))

            # Listen + periodic WS re-snapshot.
            listen = asyncio.create_task(self._ws_listen(ws))
            resnap = asyncio.create_task(self._ws_resnapshot_loop(ws))
            try:
                await asyncio.gather(listen, resnap)
            finally:
                listen.cancel()
                resnap.cancel()

    async def _ws_listen(self, ws) -> None:
        async for raw in ws:
            t_recv = int(time.time() * 1000)
            frame = json.loads(raw)
            msg_type = frame.get("type")
            msg = frame.get("msg", {})

            if msg_type == "orderbook_snapshot":
                # Let book_mgr handle live state.
                self.book_mgr.process_frame(frame)

            elif msg_type == "orderbook_delta":
                self.book_mgr.process_frame(frame)
                # Write raw delta to bronze.
                self.writer.write_delta({
                    "market_ticker": msg.get("market_ticker"),
                    "ts": int(msg.get("ts", t_recv)),
                    "side": msg.get("side"),
                    "price_cents": dollars_to_cents(msg["price_dollars"]),
                    "delta": parse_size(msg["delta_fp"]),
                    "seq": frame.get("seq"),
                    "sid": frame.get("sid"),
                })

            elif msg_type == "trade":
                yes_price = dollars_to_cents(msg["yes_price_dollars"])
                self.writer.write_trade({
                    "market_ticker": msg.get("market_ticker"),
                    "ts": int(msg.get("ts", t_recv)),
                    "yes_price_cents": yes_price,
                    "no_price_cents": 100 - yes_price,
                    "count": parse_size(msg["count_fp"]),
                    "taker_side": msg.get("taker_side"),
                })

    async def _ws_resnapshot_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(self.config.ws_resnapshot_s)
            for ticker in self._tickers:
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

    # -- REST snapshot loop ----------------------------------------------

    async def _rest_snapshot_loop(self) -> None:
        """Poll REST API for full orderbook snapshots on a cycle."""
        while self._running:
            await asyncio.sleep(self.config.snapshot_interval_s)
            log.info("Starting REST snapshot cycle for %d tickers", len(self._tickers))
            snapshots = await asyncio.to_thread(
                self.rest.get_orderbooks_batch,
                self._tickers,
            )
            for snap in snapshots:
                self.writer.write_snapshot_levels(snap)
            log.info("REST snapshot cycle complete: %d snapshots", len(snapshots))

    # -- Periodic flush --------------------------------------------------

    async def _flush_loop(self) -> None:
        """Periodically flush writer buffers to S3."""
        while self._running:
            await asyncio.sleep(self.config.writer_config.flush_interval_s)
            self.writer.flush_all()
