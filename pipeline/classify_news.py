"""
classify_news.py — raccoglie notizie da Alpha Vantage per topic

Per ogni topic in topics.json:
  - Scarica articoli dal giorno successivo all'ultima raccolta fino a ieri
  - Dedup per URL — un articolo multi-topic viene salvato una volta sola
  - Salva tutto in data/news.json con metadata di ultima raccolta per topic

Struttura data/news.json:
  {
    "last_updated": { "technology": "2026-06-02", ... },
    "articles": [ { "date", "headline", "summary", "source", "url",
                    "overall_sentiment", "topics", "all_tickers" }, ... ]
  }

Usage:
    python classify_news.py                          # tutti i topic in rotazione
    python classify_news.py --topic technology       # singolo topic
    python classify_news.py --from 2026-01-01        # backfill da data specifica
"""

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

from utils import (
    DATASET_START, DATA_DIR, NEWS_F, load_topics_config
)

load_dotenv()
AV_KEY = os.getenv("ALPHAVANTAGE_KEY")

WINDOW_DAYS   = 30
API_CALLS_MAX = 25


# --- funzioni di I/O ---

def load_news_db() -> dict:
    if not NEWS_F.exists():
        return {"last_updated": {}, "articles": []}
    with open(NEWS_F, encoding="utf-8") as f:
        return json.load(f)


def save_news_db(db: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(NEWS_F, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def last_collected_date(topic: str, db: dict) -> date:
    d = db["last_updated"].get(topic)
    return date.fromisoformat(d) if d else DATASET_START - timedelta(days=1)


# --- funzioni API ---

def av_label(score: float) -> str:
    if score > 0.1:
        return "positive"
    if score < -0.1:
        return "negative"
    return "neutral"


def fetch_window(topic: str, time_from: str, time_to: str) -> list[dict]:
    resp = requests.get(
        "https://www.alphavantage.co/query",
        params={
            "function":  "NEWS_SENTIMENT",
            "topics":    topic,
            "time_from": time_from,
            "time_to":   time_to,
            "limit":     1000,
            "sort":      "EARLIEST",
            "apikey":    AV_KEY,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "Information" in data:
        print(f"  RATE LIMIT: {data['Information']}")
        sys.exit(1)
    return data.get("feed", [])


# --- logica di raccolta ---

def build_windows(start: date, end: date) -> list[tuple[date, date]]:
    windows = []
    cur = start
    while cur <= end:
        w_end = min(cur + timedelta(days=WINDOW_DAYS - 1), end)
        windows.append((cur, w_end))
        cur = w_end + timedelta(days=1)
    return windows


def parse_article(item: dict) -> dict:
    pub = item.get("time_published", "")
    return {
        "date":              f"{pub[0:4]}-{pub[4:6]}-{pub[6:8]}",
        "headline":          item.get("title", "").strip(),
        "summary":           item.get("summary", ""),
        "source":            item.get("source", ""),
        "url":               item.get("url", ""),
        "overall_sentiment": round(float(item.get("overall_sentiment_score", 0.0)), 3),
        "overall_label":     av_label(float(item.get("overall_sentiment_score", 0.0))),
        "topics":            [t["topic"] for t in item.get("topics", [])],
        "all_tickers":       [
            {
                "ticker":    t.get("ticker"),
                "sentiment": round(float(t.get("ticker_sentiment_score", 0.0)), 3),
                "relevance": round(float(t.get("relevance_score",        0.0)), 3),
                "label":     av_label(float(t.get("ticker_sentiment_score", 0.0))),
            }
            for t in item.get("ticker_sentiment", [])
        ],
    }


def collect_topic(topic: str, start: date, end: date, seen_urls: set) -> tuple[list[dict], int]:
    """Raccoglie articoli per un topic. Restituisce (nuovi_articoli, chiamate_usate)."""
    windows = build_windows(start, end)
    print(f"\n[{topic}] {start} → {end} | {len(windows)} finestre")

    new_articles: list[dict] = []
    for i, (w_start, w_end) in enumerate(windows, 1):
        if i > 1:
            time.sleep(1.2)
        tf = w_start.strftime("%Y%m%d") + "T0000"
        tt = w_end.strftime("%Y%m%d")   + "T2359"
        print(f"  [{i}/{len(windows)}] {w_start} → {w_end}", end="", flush=True)
        feed = fetch_window(topic, tf, tt)
        if len(feed) == 1000:
            print(f"  ⚠ cap 1000 — considera finestra più corta", end="")
        print(f"  {len(feed)} art")

        for item in feed:
            url   = item.get("url", "")
            title = item.get("title", "").strip()
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            new_articles.append(parse_article(item))

    print(f"  → {len(new_articles)} nuovi articoli")
    return new_articles, len(windows)


# --- main ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", help="Singolo topic (override rotazione)")
    parser.add_argument("--from",  dest="from_date", help="Data inizio backfill (YYYY-MM-DD)")
    args = parser.parse_args()

    if not AV_KEY:
        print("ERRORE: ALPHAVANTAGE_KEY non trovata in .env o environment")
        sys.exit(1)

    config  = load_topics_config()
    topics  = [args.topic] if args.topic else config["topics"]
    end     = date.today() - timedelta(days=1)

    db        = load_news_db()
    seen_urls = {a["url"] for a in db["articles"]}

    # Ordina per staleness — topic più indietro vengono processati per primi
    if not args.topic:
        topics = sorted(topics, key=lambda t: last_collected_date(t, db))

    calls_used = 0
    for topic in topics:
        if calls_used >= API_CALLS_MAX:
            print(f"\nBudget esaurito ({API_CALLS_MAX} call). Topic rimanenti rinviati al prossimo run.")
            break

        start = date.fromisoformat(args.from_date) if args.from_date else last_collected_date(topic, db) + timedelta(days=1)

        if start > end:
            print(f"\n[{topic}] già aggiornato, skip.")
            continue

        calls_needed = len(build_windows(start, end))
        if calls_used + calls_needed > API_CALLS_MAX:
            print(f"\n[{topic}] richiederebbe {calls_needed} call, ne restano {API_CALLS_MAX - calls_used}. Skip.")
            continue

        new_articles, calls = collect_topic(topic, start, end, seen_urls)
        db["articles"].extend(new_articles)
        # Aggiorna last_updated solo se la raccolta ha prodotto risultati o
        # ha completato tutte le finestre senza errori
        db["last_updated"][topic] = end.isoformat()
        calls_used += calls

    # Ordina per data e salva
    db["articles"].sort(key=lambda x: x["date"])
    save_news_db(db)
    print(f"\nArticoli totali nel dataset: {len(db['articles'])}")
    print(f"Chiamate API usate: {calls_used}/{API_CALLS_MAX}")


if __name__ == "__main__":
    main()
