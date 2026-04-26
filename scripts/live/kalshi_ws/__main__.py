"""Live Kalshi WS ingester → bronze S3.

Connects to Kalshi's WebSocket, subscribes to `orderbook_delta` and
`trade` for every currently-open market across the NBA series listed in
``SERIES_TICKERS`` (game/spread/total/player-points), and archives each
raw frame to bronze via BronzeWriter. No transform, no silver — just
archival.

Kalshi requires an authenticated connection even for public market data,
so the handshake signs `timestamp + "GET" + path` with RSA-PSS using the
same credentials as the REST client (`KALSHI_API_KEY_ID`,
`KALSHI_PRIVATE_KEY_PATH`).

One subscribe command is issued per series (Kalshi's subscribe API takes
``market_tickers`` but not ``series_ticker``), so each series lands on
its own ``sid``. Bronze channels are keyed by the server-provided
message `type` (`orderbook_snapshot`, `orderbook_delta`, `trade`, `ok`,
`error`, ...), so snapshot/delta/trade splits fall out naturally and
control messages land under their own tiny prefixes.

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
import uuid
from pathlib import Path

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

from app.clients.kalshi_sdk import make_client, paginate_markets
from app.clients.kalshi_rest_orders import KalshiOrderClient
from app.services.bronze_writer import BronzeWriter
from app.services.silver_writer import SilverWriter
from app.strategy.mm import MMConfig, MMStrategy, PaperOrderClient
from app.transforms.kalshi_ws import KalshiTransform

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
PRIVATE_CHANNELS = ["fill", "user_orders", "market_lifecycle_v2", "market_positions"]

RECONNECT_INITIAL = 1.0
RECONNECT_MAX = 60.0
NO_MARKETS_BACKOFF = 300.0  # 5 min when every series has nothing open

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


def _fp_to_int(fp_str: str) -> int:
    """Convert Kalshi fixed-point string like '10.00' to int."""
    return int(round(float(fp_str)))


def _dollars_to_cents(dollar_str: str) -> int:
    """Convert Kalshi dollar string like '0.4500' to cents."""
    return int(round(float(dollar_str) * 100))


class Ingester:
    def __init__(
        self,
        bronze: BronzeWriter,
        transform: KalshiTransform | None = None,
        strategy: MMStrategy | None = None,
        silver: SilverWriter | None = None,
        live: bool = False,
    ) -> None:
        self.bronze = bronze
        self._transform = transform
        self._strategy = strategy
        self._silver = silver
        self._live = live
        self._shutdown = asyncio.Event()
        self._ws: websockets.ClientConnection | None = None
        self._conn_id: str | None = None
        self._reconcile_task: asyncio.Task | None = None

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

                # Phase 2: subscribe to private channels (global, no tickers)
                if self._live and self._strategy is not None:
                    for ch in PRIVATE_CHANNELS:
                        await ws.send(json.dumps({
                            "id": msg_id,
                            "cmd": "subscribe",
                            "params": {"channels": [ch]},
                        }))
                        msg_id += 1
                    log.info(
                        "subscribed to private channels: %s",
                        ", ".join(PRIVATE_CHANNELS),
                    )

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
                "conn_id": self._conn_id,
                "frame": frame,
            },
            channel=channel,
        )

        # --- Phase 2: dispatch private WS channel messages ---
        if self._live and self._strategy is not None:
            if channel in ("fill", "user_order", "market_lifecycle_v2",
                           "market_position"):
                try:
                    self._dispatch_private(frame)
                except Exception:
                    log.exception("private dispatch error on %s", channel)
                # Drain strategy events from private channel handlers
                if self._silver is not None:
                    for se in self._strategy.pending_events:
                        await self._silver.emit(se)
                    self._strategy.pending_events.clear()
                return  # private messages don't go through transform

        # --- Transform + strategy + silver ---
        if self._transform is not None:
            try:
                events = self._transform(frame, t_receipt, conn_id=self._conn_id)
            except Exception:
                log.exception("transform error on %s", channel)
                return
            for event in events:
                if self._strategy is not None:
                    try:
                        self._strategy.on_event(event)
                    except Exception:
                        log.exception("strategy error on %s", type(event).__name__)
                if self._silver is not None:
                    await self._silver.emit(event)
            # Drain any strategy-generated events (MMQuoteEvent, MMFillEvent, etc.)
            if self._strategy is not None and self._silver is not None:
                for se in self._strategy.pending_events:
                    await self._silver.emit(se)
                self._strategy.pending_events.clear()

    def _dispatch_private(self, frame: dict) -> None:
        """Route private WS channel messages to strategy callbacks."""
        assert self._strategy is not None
        msg_type = frame.get("type")
        msg = frame.get("msg", {})

        if msg_type == "fill":
            order_id = msg["order_id"]
            loc = self._strategy._order_id_map.get(order_id)
            if loc is None:
                log.warning("fill for unknown order_id %s — skipping", order_id)
                return
            ticker, side = loc

            expected_action = "buy" if side == "bid" else "sell"
            actual_action = msg.get("action")
            if actual_action != expected_action:
                log.error(
                    "fill action mismatch: expected %s, got %s for %s %s",
                    expected_action, actual_action, ticker, side,
                )
                return

            self._strategy.on_ws_fill(
                ticker=ticker,
                side=side,
                order_id=order_id,
                count=_fp_to_int(msg["count_fp"]),
                price_cents=_dollars_to_cents(msg["yes_price_dollars"]),
                fee_cents=_dollars_to_cents(msg.get("fee_cost", "0")),
                is_taker=msg.get("is_taker", False),
                post_position=_fp_to_int(msg["post_position_fp"]),
                t_ms=msg.get("ts_ms", int(time.time() * 1000)),
            )

        elif msg_type == "user_order":
            status = msg.get("status")
            order_id = msg.get("order_id")
            client_order_id = msg.get("client_order_id")

            if status == "resting":
                loc = self._strategy._client_order_map.get(
                    client_order_id,
                )
                if loc is None:
                    log.warning(
                        "ACK for unknown client_order_id %s", client_order_id,
                    )
                    return
                ticker, side = loc
                self._strategy.on_order_ack(
                    ticker, side, order_id,
                    client_order_id=client_order_id,
                )

            elif status == "canceled":
                loc = self._strategy._order_id_map.get(order_id)
                if loc is None:
                    log.warning(
                        "cancel ACK for unknown order_id %s", order_id,
                    )
                    return
                ticker, side = loc
                self._strategy.on_cancel_ack(ticker, side)

            elif status in ("executed",):
                pass  # fills arrive via the fill channel

        elif msg_type == "market_lifecycle_v2":
            event_type = msg.get("event_type")
            ticker = msg.get("market_ticker", "")
            if not ticker.startswith(self._strategy._config.series_filter):
                return
            if event_type in ("deactivated", "determined", "settled"):
                self._strategy.on_market_close(ticker, event_type)

        elif msg_type == "market_position":
            ticker = msg.get("market_ticker", "")
            position = _fp_to_int(msg.get("position_fp", "0"))
            self._strategy.on_position_update(ticker, position)

    def shutdown(self) -> None:
        log.info("shutdown requested")
        self._shutdown.set()
        if self._strategy is not None:
            self._strategy.stop()
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
        ws = self._ws
        if ws is not None:
            asyncio.create_task(ws.close())


async def _main() -> None:
    transform = KalshiTransform()
    strategy: MMStrategy | None = None
    order_client: KalshiOrderClient | None = None

    mm_enabled = os.environ.get("MM_ENABLED", "0") == "1"
    mm_live = os.environ.get("MM_LIVE", "0") == "1"

    if mm_enabled:
        config = MMConfig(state_path=Path("mm_state.json"))
        if mm_live:
            # Phase 2: live trading
            order_client = KalshiOrderClient(
                circuit_breaker_threshold=config.circuit_breaker_threshold,
            )
            strategy = MMStrategy(
                order_client=order_client,  # type: ignore[arg-type]
                config=config,
                live=True,
            )
            log.info("MM strategy enabled (LIVE mode): %s", config)
        else:
            # Phase 1: paper trading
            strategy = MMStrategy(order_client=None, config=config)  # type: ignore[arg-type]
            paper_client = PaperOrderClient(strategy)
            strategy._client = paper_client
            log.info("MM strategy enabled (paper mode): %s", config)

    async with BronzeWriter(source="kalshi_ws") as bronze, \
               SilverWriter(source="kalshi_ws", flush_seconds=60) as silver:
        ingester = Ingester(
            bronze,
            transform=transform,
            strategy=strategy if mm_enabled else None,
            silver=silver,
            live=mm_live,
        )

        # Phase 2: bootstrap + reconciliation loop
        if mm_live and strategy is not None:
            await strategy.bootstrap()
            ingester._reconcile_task = asyncio.create_task(
                strategy.reconciliation_loop(),
            )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, ingester.shutdown)

        try:
            await ingester.run()
        finally:
            # Clean shutdown: cancel all resting orders
            if mm_live and order_client is not None and strategy is not None:
                log.info("shutting down — cancelling all resting orders")
                for ticker, sides in strategy._order_state.items():
                    for side_name, state in sides.items():
                        if state.state == "resting" and state.order_id:
                            try:
                                await order_client.cancel(state.order_id)
                            except Exception:
                                log.exception(
                                    "failed to cancel %s on shutdown",
                                    state.order_id,
                                )
                if order_client is not None:
                    order_client.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main())


if __name__ == "__main__":
    main()
