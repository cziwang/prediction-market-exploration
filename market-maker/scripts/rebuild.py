"""Batch reconstruct silver LOB from bronze data for a given date."""

import logging
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.reconstruct import ReconstructConfig, reconstruct_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rebuild")


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m market-maker.scripts.rebuild YYYY-MM-DD [YYYY-MM-DD ...]")
        sys.exit(1)

    dates = [arg for arg in sys.argv[1:] if not arg.startswith("--")]

    config = ReconstructConfig()

    for date_str in dates:
        log.info("Reconstructing %s...", date_str)
        reconstruct_day(date_str, config)

    log.info("Done.")


if __name__ == "__main__":
    main()
