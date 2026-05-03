"""Start live data collection: WS + REST → S3 bronze."""

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

# Add parent to path for imports.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.collector import Collector, CollectorConfig
from src.parquet_writer import WriterConfig
from src.rest_client import DEMO_REST_URL, PROD_REST_URL
from src.collector import DEMO_WS_URL, PROD_WS_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collect")

# Crypto series on Kalshi.
CRYPTO_SERIES = ["KXBTC", "KXETH", "KXSOL"]


def main():
    parser = argparse.ArgumentParser(description="Kalshi data collector")
    parser.add_argument("--prod", action="store_true", help="Use production API")
    parser.add_argument(
        "--series",
        nargs="*",
        default=CRYPTO_SERIES,
        help="Series tickers to subscribe to (default: crypto). "
             "Pass --series with no args to discover ALL markets.",
    )
    args = parser.parse_args()

    ws_url = PROD_WS_URL if args.prod else DEMO_WS_URL
    rest_url = PROD_REST_URL if args.prod else DEMO_REST_URL
    env = "PROD" if args.prod else "DEMO"
    series = args.series if args.series else []

    log.info("Starting collector on %s (series=%s)", env, series or "ALL")

    config = CollectorConfig(
        ws_url=ws_url,
        rest_base_url=rest_url,
        auto_discover=True,
        series_tickers=series,
        writer_config=WriterConfig(),
    )

    collector = Collector(config)

    try:
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
