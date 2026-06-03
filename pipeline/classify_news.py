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
FEED_CAP      = 1000  # max articoli che l'API restituisce per finestra


class APIError(Exception):
    """Errore generico restituito dall'API Alpha Vantage."""


class RateLimit(APIError):
    """Budget giornaliero esaurito — fermarsi e salvare il progresso."""


# --- funzioni di I/O ---

def load_news_db() -> dict:
    if not NEWS_F.exists():
        return {"last_updated": {}, "articles": []}
    with open(NEWS_F, encoding="utf-8") as f:
        return json.load(f)


def save_news_db(db: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Scrittura atomica: scrivo su un file temporaneo e poi lo rinomino.
    # Se il processo viene ucciso a metà scrittura (timeout CI, OOM) il file
    # definitivo resta intatto e non viene committato un JSON troncato.
    tmp = NEWS_F.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, NEWS_F)


def last_collected_date(topic: str, db: dict) -> date:
    d = db["last_updated"].get(topic)
    return date.fromisoformat(d) if d else DATASET_START - timedelta(days=1)


# --- funzioni API ---

def to_float(x, default: float = 0.0) -> float:
    """Converte in float tollerando None e stringhe vuote/non numeriche.
    L'API a volte restituisce null al posto di uno score: float(None) fallirebbe."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


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
    # Alpha Vantage segnala il throttling con "Information" o "Note",
    # gli errori di richiesta con "Error Message". In tutti i casi "feed"
    # è assente: non vanno trattati come "zero articoli".
    if "Information" in data or "Note" in data:
        raise RateLimit(data.get("Information") or data.get("Note"))
    if "Error Message" in data:
        raise APIError(data["Error Message"])
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
    pub     = item.get("time_published", "")
    overall = to_float(item.get("overall_sentiment_score"))
    return {
        "date":              f"{pub[0:4]}-{pub[4:6]}-{pub[6:8]}",
        "headline":          item.get("title", "").strip(),
        "summary":           item.get("summary", ""),
        "source":            item.get("source", ""),
        "url":               item.get("url", ""),
        "overall_sentiment": round(overall, 3),
        "overall_label":     av_label(overall),
        "topics":            [t["topic"] for t in item.get("topics", [])],
        "all_tickers":       [
            {
                "ticker":    t.get("ticker"),
                "sentiment": round(to_float(t.get("ticker_sentiment_score")), 3),
                "relevance": round(to_float(t.get("relevance_score")), 3),
                "label":     av_label(to_float(t.get("ticker_sentiment_score"))),
            }
            for t in item.get("ticker_sentiment", [])
        ],
    }


def fetch_window_complete(topic: str, w_start: date, w_end: date) -> tuple[list[dict], int]:
    """Scarica una finestra. Se satura il cap (FEED_CAP articoli) la divide a
    metà e ricorre, così non si perdono articoli oltre il limite dell'API.
    La ricorsione si ferma al singolo giorno: se un giorno satura il cap il
    troncamento è un limite reale dell'API, non un bug nostro.
    Restituisce (articoli, chiamate_API_usate)."""
    tf = w_start.strftime("%Y%m%d") + "T0000"
    tt = w_end.strftime("%Y%m%d")   + "T2359"
    feed  = fetch_window(topic, tf, tt)
    calls = 1

    if len(feed) >= FEED_CAP and w_start < w_end:
        mid = w_start + (w_end - w_start) // 2
        print(f"\n    ↳ cap {FEED_CAP} su {w_start}→{w_end}, divido", end="", flush=True)
        time.sleep(1.2)
        left,  lc = fetch_window_complete(topic, w_start, mid)
        time.sleep(1.2)
        right, rc = fetch_window_complete(topic, mid + timedelta(days=1), w_end)
        return left + right, calls + lc + rc

    if len(feed) >= FEED_CAP:
        print(f"  ⚠ {w_start} satura il cap {FEED_CAP} — possibile troncamento", end="")
    return feed, calls


def collect_topic(topic: str, start: date, end: date, seen_urls: set, db: dict) -> int:
    """Raccoglie articoli per un topic, scrivendo in `db` finestra per finestra.
    `last_updated[topic]` avanza solo dopo che una finestra è stata scaricata
    interamente: se la raccolta si interrompe (rate limit, rete), il progresso
    già committato in `db` è coerente e ripartirà senza buchi né sovrapposizioni.
    Restituisce il numero di chiamate API usate."""
    windows = build_windows(start, end)
    print(f"\n[{topic}] {start} → {end} | {len(windows)} finestre")

    calls_used = 0
    added = 0
    for i, (w_start, w_end) in enumerate(windows, 1):
        if i > 1:
            time.sleep(1.2)
        print(f"  [{i}/{len(windows)}] {w_start} → {w_end}", end="", flush=True)
        feed, calls = fetch_window_complete(topic, w_start, w_end)
        calls_used += calls
        print(f"  {len(feed)} art")

        for item in feed:
            url   = item.get("url", "")
            title = item.get("title", "").strip()
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            db["articles"].append(parse_article(item))
            added += 1

        # Finestra completata: avanza il checkpoint del topic.
        db["last_updated"][topic] = w_end.isoformat()

    print(f"  → {added} nuovi articoli")
    return calls_used


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

        try:
            calls_used += collect_topic(topic, start, end, seen_urls, db)
        except RateLimit as e:
            # collect_topic ha già committato in `db` le finestre completate:
            # salviamo e ci fermiamo senza perdere il lavoro fatto finora.
            print(f"\nRATE LIMIT: {e}")
            print("Salvo il progresso e mi fermo — il resto al prossimo run.")
            break
        finally:
            # Persisti dopo ogni topic così un crash non vanifica il budget speso.
            db["articles"].sort(key=lambda x: x["date"])
            save_news_db(db)

    print(f"\nArticoli totali nel dataset: {len(db['articles'])}")
    print(f"Chiamate API usate: {calls_used}/{API_CALLS_MAX}")


if __name__ == "__main__":
    main()
