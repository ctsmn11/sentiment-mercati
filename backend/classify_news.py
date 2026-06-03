"""
classify_news.py — scarica e classifica notizie da Alpha Vantage NEWS_SENTIMENT API

Usage:
    python classify_news.py                    # ^GSPC, ultimi 60 giorni
    python classify_news.py --ticker ^GSPC --days 90

Output: data/news_raw.json + data/news_classified.json (sentiment gia' incluso)
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import time

import requests
from dotenv import load_dotenv

load_dotenv()
AV_KEY = os.getenv("ALPHAVANTAGE_KEY")
DEFAULT_TICKER = os.getenv("TICKER", "^GSPC")

# Alpha Vantage usa simboli ETF, non indici
AV_TICKER_MAP = {
    "^GSPC": "SPY",
    "^IXIC": "QQQ",
    "^DJI":  "DIA",
}

TICKER_NAMES = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq 100",
    "^DJI":  "Dow Jones",
}


def av_sentiment_to_label(score: float) -> str:
    if score > 0.1:
        return "positive"
    if score < -0.1:
        return "negative"
    return "neutral"


WINDOW_DAYS = 30  # articoli per finestra (free tier: 50 per chiamata)


def _fetch_window(av_ticker: str, time_from: str, time_to: str) -> list[dict]:
    resp = requests.get(
        "https://www.alphavantage.co/query",
        params={
            "function": "NEWS_SENTIMENT",
            "tickers": av_ticker,
            "topics": "financial_markets,economy_macro",
            "time_from": time_from,
            "time_to": time_to,
            "limit": 50,
            "sort": "EARLIEST",
            "apikey": AV_KEY,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "Information" in data:
        print(f"ERRORE API: {data['Information']}")
        sys.exit(1)
    return data.get("feed", [])


def fetch_and_classify(ticker: str, start: str, end: str) -> list[dict]:
    av_ticker = AV_TICKER_MAP.get(ticker.upper(), ticker)
    name = TICKER_NAMES.get(ticker.upper(), ticker)

    print(f"Ticker  : {ticker} ({name})")
    print(f"AV sym  : {av_ticker}")
    print(f"Periodo : {start} -> {end}")

    # Suddivide il range in finestre da WINDOW_DAYS per aggirare il limite di 50 art/chiamata
    windows = []
    cur = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    while cur <= end_d:
        w_end = min(cur + timedelta(days=WINDOW_DAYS - 1), end_d)
        windows.append((cur.isoformat(), w_end.isoformat()))
        cur = w_end + timedelta(days=1)

    print(f"Finestre: {len(windows)} x {WINDOW_DAYS}gg  (chiamate API usate: {len(windows)}/25 giornaliere)")

    seen: set[str] = set()
    articles: list[dict] = []

    for i, (w_start, w_end) in enumerate(windows, 1):
        if i > 1:
            time.sleep(1.2)
        tf = w_start.replace("-", "") + "T0000"
        tt = w_end.replace("-", "") + "T2359"
        print(f"  [{i}/{len(windows)}] {w_start} -> {w_end}", end="", flush=True)
        feed = _fetch_window(av_ticker, tf, tt)
        print(f"  {len(feed)} art")

        for item in feed:
            title = item.get("title", "").strip()
            pub = item.get("time_published", "")
            if not title or not pub or title in seen:
                continue
            seen.add(title)

            article_date = f"{pub[0:4]}-{pub[4:6]}-{pub[6:8]}"
            ts_list = item.get("ticker_sentiment", [])
            ts = next((t for t in ts_list if t.get("ticker") == av_ticker), None)

            if ts:
                relevance = round(float(ts.get("relevance_score", 0.5)), 3)
                sentiment = round(float(ts.get("ticker_sentiment_score", 0.0)), 3)
            else:
                relevance = 0.5
                sentiment = round(float(item.get("overall_sentiment_score", 0.0)), 3)

            articles.append({
                "date":      article_date,
                "headline":  title,
                "relevance": relevance,
                "sentiment": sentiment,
                "label":     av_sentiment_to_label(sentiment),
            })

    return sorted(articles, key=lambda x: x["date"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default=DEFAULT_TICKER)
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()

    if not AV_KEY:
        print("ERRORE: ALPHAVANTAGE_KEY non trovata in backend/.env")
        sys.exit(1)

    today = date.today()
    end_date = today.isoformat()
    start_date = (today - timedelta(days=args.days)).isoformat()

    articles = fetch_and_classify(args.ticker, start_date, end_date)

    print(f"\nArticoli trovati: {len(articles)}")
    from collections import Counter
    by_date = Counter(a["date"] for a in articles)
    for d in sorted(by_date):
        print(f"  {d}: {by_date[d]} art")

    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)

    # news_raw.json — solo headline, per riferimento
    raw = {
        "ticker": args.ticker,
        "start": start_date,
        "end": end_date,
        "article_count": len(articles),
        "articles": [{"date": a["date"], "headline": a["headline"]} for a in articles],
    }
    with open(data_dir / "news_raw.json", "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    # news_classified.json — con sentiment gia' calcolato da Alpha Vantage
    classified = {
        "ticker": args.ticker,
        "start": start_date,
        "end": end_date,
        "articles": articles,
    }
    with open(data_dir / "news_classified.json", "w", encoding="utf-8") as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)

    print(f"\nSalvato -> data/news_raw.json + data/news_classified.json")

    pos = sum(1 for a in articles if a["label"] == "positive")
    neg = sum(1 for a in articles if a["label"] == "negative")
    neu = sum(1 for a in articles if a["label"] == "neutral")
    print(f"Sentiment: {pos} positive, {neg} negative, {neu} neutral")


if __name__ == "__main__":
    main()
