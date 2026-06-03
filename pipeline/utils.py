"""
utils.py — costanti e funzioni condivise tra gli script della pipeline
"""

import json
from datetime import date
from pathlib import Path

BASE_DIR      = Path(__file__).parent
DATA_DIR      = BASE_DIR / "data"
TICKERS_F     = BASE_DIR / "tickers.json"
TOPICS_F      = BASE_DIR / "topics.json"
PENDING_F     = BASE_DIR / "tickers_pending.json"
NEWS_F        = DATA_DIR / "news.json"       # dataset articoli (topic-based)
DATASET_START = date(2026, 1, 1)


def load_tickers_config() -> dict:
    with open(TICKERS_F, encoding="utf-8") as f:
        return json.load(f)


def load_topics_config() -> dict:
    with open(TOPICS_F, encoding="utf-8") as f:
        return json.load(f)


def all_tickers(config: dict) -> list[str]:
    return config["tickers"] + [config["market_index"]]


def ticker_data_dir(ticker: str) -> Path:
    return DATA_DIR / ticker


def bump_version(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{int(minor) + 1}.0"
