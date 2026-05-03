import os

from dotenv import load_dotenv

load_dotenv()

S3_BUCKET = os.getenv("S3_BUCKET", "prediction-markets-data")

SILVER_VERSION = 3
