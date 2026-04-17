import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SERIES = "KXNBAGAME"
ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "kalshi_nba.db"

S3_BUCKET = os.getenv("S3_BUCKET", "prediction-markets-data")

POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql://localhost:5432/prediction_markets",
)
