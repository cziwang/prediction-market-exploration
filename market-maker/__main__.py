"""Entry point: connect to Kalshi WS and maintain L2 books."""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from .src.book import BookManager
from .src.ws_client import DEMO_WS_URL, PROD_WS_URL, KalshiWSClient, WSConfig

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("market-maker")


# Example tickers — replace with your target markets.
DEFAULT_TICKERS = [
    # Add tickers here, e.g. "KXBTC-25MAY0300-T103999.99",
]


def main():
    use_prod = "--prod" in sys.argv
    url = PROD_WS_URL if use_prod else DEMO_WS_URL
    env_label = "PROD" if use_prod else "DEMO"
    log.info("Starting market-maker on %s (%s)", env_label, url)

    tickers = DEFAULT_TICKERS
    if not tickers:
        log.error("No tickers configured. Add tickers to DEFAULT_TICKERS or pass via config.")
        sys.exit(1)

    config = WSConfig(url=url, tickers=tickers)
    book_mgr = BookManager()
    client = KalshiWSClient(config, book_mgr)

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
