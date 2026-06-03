import json
from datetime import date, timedelta
from pathlib import Path

import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

CLASSIFIED_FILE = Path(__file__).parent / "data" / "news_classified.json"

TICKER_ALIASES = {
    "^VWCE": "VWCE.AS",
}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _load_classified() -> tuple[list[dict], str]:
    if not CLASSIFIED_FILE.exists():
        raise HTTPException(
            status_code=503,
            detail="news_classified.json non trovato. Esegui prima: python classify_news.py",
        )
    with open(CLASSIFIED_FILE, encoding="utf-8") as f:
        data = json.load(f)
    ticker = data.get("ticker", "^GSPC")
    articles = [
        {
            "date": a["date"],
            "headline": a["headline"],
            "score": round(float(a["sentiment"]) * float(a["relevance"]), 4),
            "sentiment": a.get("label", "neutral"),
        }
        for a in data.get("articles", [])
    ]
    return articles, ticker


def _fetch_prices(start: str, end: str, ticker: str) -> list[dict]:
    yf_ticker = TICKER_ALIASES.get(ticker, ticker)
    end_exclusive = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    df = yf.Ticker(yf_ticker).history(start=start, end=end_exclusive, auto_adjust=True)
    if df.empty:
        return []
    return [
        {"date": str(idx.date()), "close": round(float(row["Close"]), 2)}
        for idx, row in df.iterrows()
    ]


_cache: dict | None = None


@app.get("/api/data")
def get_data():
    global _cache
    if _cache is not None:
        return _cache

    news, ticker = _load_classified()
    dates = [n["date"] for n in news]
    prices = _fetch_prices(min(dates), max(dates), ticker)

    _cache = {"news": news, "prices": prices}
    return _cache
