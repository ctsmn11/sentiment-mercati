"""
utils.py — costanti e funzioni condivise tra gli script della pipeline
"""

import json
import os
from datetime import date
from pathlib import Path

from google.cloud import storage

BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "data"            # usato solo dall'EDA notebook in locale
TICKERS_F     = BASE_DIR / "tickers.json"
NEWS_F        = DATA_DIR / "news.json"       # usato solo dall'EDA notebook in locale
DATASET_START = date(2026, 1, 1)
BUCKET_NAME   = os.getenv("GCS_BUCKET", "sentiment-mercati-data")

# Percorsi dei blob nel bucket GCS
NEWS_BLOB = "news.json"

def prices_blob(ticker: str) -> str:
    return f"{ticker}/prices.json"


# --- GCS helpers ---

def gcs_download_json(blob_name: str, default=None) -> dict:
    """Scarica e parsa un blob JSON dal bucket. Restituisce default se non esiste."""
    client = storage.Client()
    blob = client.bucket(BUCKET_NAME).blob(blob_name)
    if not blob.exists():
        return default if default is not None else {}
    return json.loads(blob.download_as_text(encoding="utf-8"))


def gcs_upload_json(blob_name: str, data):
    """Carica data come JSON nel bucket. L'upload GCS è atomico per natura."""
    client = storage.Client()
    blob = client.bucket(BUCKET_NAME).blob(blob_name)
    blob.upload_from_string(
        json.dumps(data, ensure_ascii=False, indent=2),
        content_type="application/json",
    )


# --- funzioni condivise ---

def load_tickers_config() -> dict:
    with open(TICKERS_F, encoding="utf-8") as f:
        return json.load(f)


def all_tickers(config: dict) -> list[str]:
    return config["tickers"] + [config["market_index"]]


def ticker_data_dir(ticker: str) -> Path:
    return DATA_DIR / ticker


def bump_version(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{int(minor) + 1}.0"
