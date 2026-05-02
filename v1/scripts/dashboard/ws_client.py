"""Background WebSocket client for the trading dashboard.

Connects to Kalshi WS, subscribes to public + private channels, and
maintains shared in-memory state that the Streamlit UI reads.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import websockets.sync.client as ws_sync
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

from app.clients.kalshi_sdk import make_client, paginate_markets
from app.transforms.kalshi_ws import OrderBookState, _dollars_to_cents

load_dotenv()

log = logging.getLogger(__name__)

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"

PUBLIC_CHANNELS = ["orderbook_delta", "trade"]
PRIVATE_CHANNELS = ["fill", "user_orders", "market_positions", "market_lifecycle_v2"]


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
        raise RuntimeError("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set")
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


def _fetch_open_kxnbapts_tickers() -> list[str]:
    api = make_client()
    return [m.ticker for m in paginate_markets(api, series_ticker="KXNBAPTS", status="open")]


class DashboardState:
    """Thread-safe shared state between WS thread and Streamlit UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Public channel state
        self.books: dict[str, OrderBookState] = {}
        self.recent_trades: deque[dict] = deque(maxlen=500)
        # Private channel state
        self.fills: list[dict] = []
        self.positions: dict[str, dict] = {}  # ticker -> market_positions msg
        self.resting_orders: dict[str, dict] = {}  # order_id -> user_orders msg
        self.market_status: dict[str, str] = {}  # ticker -> event_type
        # Connection status
        self.connected: bool = False
        self.last_msg_time: float = 0.0
        self.subscribed_tickers: list[str] = []
        self.error: str | None = None

    @property
    def lock(self) -> threading.Lock:
        return self._lock


class KalshiWSClient:
    """Synchronous WS client that runs in a background thread."""

    def __init__(self, state: DashboardState) -> None:
        self._state = state
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect_once()
                backoff = 1.0
            except Exception as e:
                log.exception("WS connection failed")
                with self._state.lock:
                    self._state.connected = False
                    self._state.error = str(e)
                backoff = min(backoff * 2, 30.0)
            if not self._stop.is_set():
                self._stop.wait(timeout=backoff)

    def _connect_once(self) -> None:
        # Fetch open tickers
        tickers = _fetch_open_kxnbapts_tickers()
        if not tickers:
            log.warning("No open KXNBAPTS markets")
            with self._state.lock:
                self._state.error = "No open KXNBAPTS markets"
            self._stop.wait(timeout=60)
            return

        with self._state.lock:
            self._state.subscribed_tickers = tickers
            self._state.error = None

        headers = _build_auth_headers()
        with ws_sync.connect(
            WS_URL,
            additional_headers=headers,
            close_timeout=5,
        ) as ws:
            with self._state.lock:
                self._state.connected = True
                self._state.error = None

            # Subscribe to public channels in batches (Kalshi limits per message)
            msg_id = 1
            batch_size = 50
            for i in range(0, len(tickers), batch_size):
                batch = tickers[i : i + batch_size]
                ws.send(json.dumps({
                    "id": msg_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": PUBLIC_CHANNELS,
                        "market_tickers": batch,
                    },
                }))
                msg_id += 1

            # Subscribe to private channels (global, no ticker filter)
            ws.send(json.dumps({
                "id": msg_id,
                "cmd": "subscribe",
                "params": {
                    "channels": PRIVATE_CHANNELS,
                },
            }))

            log.info("Subscribed to %d tickers in %d batches + private channels",
                     len(tickers), (len(tickers) + batch_size - 1) // batch_size)

            ws.recv_bufsize = 2 ** 20  # 1MB buffer
            while not self._stop.is_set():
                try:
                    raw = ws.recv(timeout=5)
                except TimeoutError:
                    continue
                self._handle_message(raw)

    def _handle_message(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = frame.get("type", "")
        msg = frame.get("msg", {})

        with self._state.lock:
            self._state.last_msg_time = time.time()

            if msg_type == "orderbook_snapshot":
                ticker = msg.get("market_ticker")
                if ticker:
                    self._state.books[ticker] = OrderBookState.from_snapshot(msg, min_size=1)

            elif msg_type == "orderbook_delta":
                ticker = msg.get("market_ticker")
                if ticker and ticker in self._state.books:
                    self._state.books[ticker].apply_delta(msg)

            elif msg_type == "trade":
                ticker = msg.get("market_ticker")
                if ticker:
                    self._state.recent_trades.appendleft({
                        "t": time.time(),
                        "ticker": ticker,
                        "side": msg.get("taker_side", ""),
                        "price": _dollars_to_cents(str(msg.get("yes_price_dollars", "0"))),
                        "size": int(round(float(msg.get("count_fp", "0")))),
                    })

            elif msg_type == "fill":
                fill_ticker = msg.get("market_ticker", "")
                self._state.fills.append({
                    "t_ms": msg.get("ts_ms", 0),
                    "trade_id": msg.get("trade_id", ""),
                    "order_id": msg.get("order_id", ""),
                    "ticker": fill_ticker,
                    "action": msg.get("action", ""),
                    "side": msg.get("side", ""),
                    "price": _dollars_to_cents(str(msg.get("yes_price_dollars", "0"))),
                    "size": int(round(float(msg.get("count_fp", "0")))),
                    "fee": float(msg.get("fee_cost", "0")),
                    "is_taker": msg.get("is_taker", False),
                    "post_position": int(round(float(msg.get("post_position_fp", "0")))),
                })

            elif msg_type == "user_order":
                order_id = msg.get("order_id", "")
                status = msg.get("status", "")
                if order_id:
                    if status in ("canceled", "executed"):
                        self._state.resting_orders.pop(order_id, None)
                    else:
                        self._state.resting_orders[order_id] = {
                            "order_id": order_id,
                            "ticker": msg.get("ticker", ""),
                            "side": msg.get("side", ""),
                            "price": _dollars_to_cents(str(msg.get("yes_price_dollars", "0"))),
                            "initial_size": int(round(float(msg.get("initial_count_fp", "0")))),
                            "remaining": int(round(float(msg.get("remaining_count_fp", "0")))),
                            "fill_count": int(round(float(msg.get("fill_count_fp", "0")))),
                            "status": status,
                            "maker_fees": float(msg.get("maker_fees_dollars", "0")),
                            "taker_fees": float(msg.get("taker_fees_dollars", "0")),
                            "updated_ms": msg.get("last_updated_ts_ms", 0),
                        }

            elif msg_type == "market_position":
                ticker = msg.get("market_ticker", "")
                if ticker:
                    self._state.positions[ticker] = {
                        "ticker": ticker,
                        "position": int(round(float(msg.get("position_fp", "0")))),
                        "cost_dollars": float(msg.get("position_cost_dollars", "0")),
                        "realized_pnl": float(msg.get("realized_pnl_dollars", "0")),
                        "fees_paid": float(msg.get("fees_paid_dollars", "0")),
                        "volume": int(round(float(msg.get("volume_fp", "0")))),
                    }

            elif msg_type == "market_lifecycle_v2":
                ticker = msg.get("market_ticker", "")
                event = msg.get("event_type", "")
                if ticker and event:
                    self._state.market_status[ticker] = event

            elif msg_type in ("subscribed", "error"):
                if msg_type == "error":
                    log.warning("WS error: %s", msg)
