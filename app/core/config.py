from pathlib import Path

SERIES = "KXNBAGAME"
ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "kalshi_nba.db"
