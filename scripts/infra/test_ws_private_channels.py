"""Test whether public + private WS channels coexist on one connection.

Connects to Kalshi's authenticated WS, subscribes to:
  - Public: orderbook_delta (requires market_tickers)
  - Private: fill, user_orders, market_positions

Listens for subscription confirmations (type="ok") and any messages
on private channels. Exits after a timeout or after receiving
confirmations for all subscriptions.

Usage:
    source .venv/bin/activate
    python -m scripts.infra.test_ws_private_channels

Requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

from app.clients.kalshi_sdk import make_client, paginate_markets

load_dotenv()

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"
LISTEN_TIMEOUT = 30  # seconds to listen after subscribing

log = logging.getLogger("test_ws_private")


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
    key_id = os.environ["KALSHI_API_KEY_ID"]
    key_path = Path(os.environ["KALSHI_PRIVATE_KEY_PATH"])
    timestamp_ms = str(int(time.time() * 1000))
    signature = _sign(key_path, timestamp_ms, "GET", WS_SIGN_PATH)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


def _get_sample_tickers(n: int = 3) -> list[str]:
    """Fetch a few open KXNBAPTS tickers to subscribe to."""
    api = make_client()
    tickers = [
        m.ticker
        for m in paginate_markets(api, series_ticker="KXNBAPTS", status="open")
    ]
    return tickers[:n]


async def _run() -> None:
    # Fetch a few tickers for the public channel subscription
    log.info("fetching sample open tickers...")
    tickers = await asyncio.to_thread(_get_sample_tickers)
    if not tickers:
        log.warning("no open KXNBAPTS markets — trying KXNBAGAME")
        tickers = await asyncio.to_thread(
            lambda: [
                m.ticker
                for m in paginate_markets(
                    make_client(), series_ticker="KXNBAGAME", status="open"
                )
            ][:3]
        )
    if not tickers:
        log.error("no open markets at all — cannot test. Try during game time.")
        return

    log.info("using tickers: %s", tickers)

    headers = _build_auth_headers()
    async with websockets.connect(
        WS_URL,
        additional_headers=headers,
        ping_interval=30,
        ping_timeout=10,
    ) as ws:
        # Subscribe to public channel with market_tickers
        await ws.send(json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        }))
        log.info("sent subscribe: orderbook_delta for %d tickers", len(tickers))

        # Subscribe to private channels (no market_tickers needed)
        private_channels = ["fill", "user_orders", "market_positions"]
        for i, ch in enumerate(private_channels, start=2):
            await ws.send(json.dumps({
                "id": i,
                "cmd": "subscribe",
                "params": {"channels": [ch]},
            }))
            log.info("sent subscribe: %s (id=%d)", ch, i)

        # Also try market_lifecycle_v2
        await ws.send(json.dumps({
            "id": len(private_channels) + 2,
            "cmd": "subscribe",
            "params": {"channels": ["market_lifecycle_v2"]},
        }))
        log.info("sent subscribe: market_lifecycle_v2 (id=%d)",
                 len(private_channels) + 2)

        # Listen for responses
        ack_ids: set[int] = set()
        error_ids: dict[int, str] = {}
        channel_labels = {
            1: "orderbook_delta (public)",
            2: "fill (private)",
            3: "user_orders (private)",
            4: "market_positions (private)",
            5: "market_lifecycle_v2 (private)",
        }
        msg_types_seen: dict[str, int] = {}
        expected_acks = set(channel_labels.keys())

        deadline = time.time() + LISTEN_TIMEOUT
        log.info("listening for %ds...", LISTEN_TIMEOUT)

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, deadline - time.time())
                )
            except asyncio.TimeoutError:
                break

            frame = json.loads(raw)
            msg_type = frame.get("type", "unknown")
            msg_types_seen[msg_type] = msg_types_seen.get(msg_type, 0) + 1

            if msg_type == "ok":
                sub_id = frame.get("id")
                ack_ids.add(sub_id)
                label = channel_labels.get(sub_id, f"unknown (id={sub_id})")
                log.info("  OK: subscription %s confirmed (sid=%s)",
                         label, frame.get("sid"))
            elif msg_type == "error":
                sub_id = frame.get("id")
                error_msg = frame.get("msg", {}).get("error", str(frame))
                error_ids[sub_id] = error_msg
                label = channel_labels.get(sub_id, f"unknown (id={sub_id})")
                log.error("  ERROR: subscription %s failed: %s", label, error_msg)
            else:
                # Data message — just count by type
                if msg_types_seen[msg_type] <= 3:
                    ticker = ""
                    if isinstance(frame.get("msg"), dict):
                        ticker = frame["msg"].get("market_ticker", "")
                    log.info("  data: type=%s ticker=%s", msg_type, ticker)

            # If all subscriptions confirmed (or errored), we can stop early
            if ack_ids | set(error_ids.keys()) >= expected_acks:
                # But keep listening a bit for data messages
                if time.time() > deadline - LISTEN_TIMEOUT + 5:
                    break

    # Summary
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    all_ok = True
    for sub_id, label in sorted(channel_labels.items()):
        if sub_id in ack_ids:
            print(f"  {label}: OK")
        elif sub_id in error_ids:
            print(f"  {label}: FAILED — {error_ids[sub_id]}")
            all_ok = False
        else:
            print(f"  {label}: NO RESPONSE")
            all_ok = False

    print(f"\nMessage types seen: {dict(sorted(msg_types_seen.items()))}")

    if all_ok:
        print("\nSingle-connection public+private channels: WORKS")
    else:
        print("\nSingle-connection public+private channels: DOES NOT WORK")
        print("Phase 2 may need separate WS connections for public vs private.")

    print("=" * 60)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
