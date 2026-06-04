"""
classify_news.py — raccoglie notizie + sentiment da Alpha Vantage per ticker

Per ogni ticker in tickers.json (costituenti + indice di mercato):
  - Scarica articoli dal giorno successivo all'ultima raccolta fino a ieri,
    in finestre da 30 giorni, UNA chiamata API per finestra.
  - Dedup per URL — un articolo che cita piu' ticker viene salvato una volta
    sola; il suo array `all_tickers` contiene comunque il sentiment per ognuno.
  - Avanza il checkpoint `last_updated[ticker]` dopo OGNI finestra completata,
    cosi' ogni chiamata produce progresso permanente: niente lavoro perso e
    niente deadlock se il budget giornaliero (25 call) finisce a meta'.

I ticker vengono processati in ordine di staleness (i piu' indietro per primi),
cosi' su piu' run il backlog si svuota a rotazione rientrando nel budget free.

Se una finestra supera FEED_CAP articoli, l'API restituisce i primi 1000 (sort=EARLIEST
= i più vecchi). Il checkpoint avanza solo fino all'ultima data trovata nel feed,
così il run successivo riparte da lì e non salta gli articoli intermedi.

Struttura data/news.json:
  {
    "last_updated": { "AAPL": "2026-06-02", ... },
    "articles": [ { "date", "headline", "summary", "source", "url",
                    "overall_sentiment", "overall_label", "topics",
                    "all_tickers" }, ... ]
  }

Usage:
    python classify_news.py                      # tutti i ticker, a rotazione
    python classify_news.py --ticker AAPL        # singolo ticker
    python classify_news.py --from 2026-01-01    # backfill da data specifica
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
    DATASET_START, NEWS_BLOB, all_tickers, load_tickers_config,
    gcs_download_json, gcs_upload_json,
)

# Su Windows la console di default è cp1252 e va in errore sui caratteri non
# ASCII (→, ⚠) usati nei log; su CI lo stdout è già UTF-8 e questo è un no-op.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

load_dotenv()
AV_KEY = os.getenv("ALPHAVANTAGE_KEY")

WINDOW_DAYS   = 30
API_CALLS_MAX = 25
FEED_CAP      = 1000  # max articoli che l'API restituisce per finestra

# Simbolo usato da Alpha Vantage NEWS_SENTIMENT, quando diverge dal nostro.
# L'indice S&P 500 non e' un ticker di news: usiamo SPY (l'ETF che lo replica)
# come proxy del "sentiment di mercato".
NEWS_TICKER_ALIASES = {"^GSPC": "SPY"}


class APIError(Exception):
    """Errore generico restituito dall'API Alpha Vantage."""


class RateLimit(APIError):
    """Budget giornaliero esaurito — fermarsi e salvare il progresso."""


def to_av_symbol(ticker: str) -> str:
    return NEWS_TICKER_ALIASES.get(ticker, ticker)


# --- funzioni di I/O ---

def load_news_db() -> dict:
    db = gcs_download_json(NEWS_BLOB, default={"last_updated": {}, "articles": []})
    db.setdefault("last_updated", {})
    db.setdefault("articles", [])
    return db


def save_news_db(db: dict):
    # L'upload su GCS è atomico: il blob precedente resta leggibile
    # fino al completamento dell'upload, senza rischio di file troncati.
    gcs_upload_json(NEWS_BLOB, db)


def last_collected_date(ticker: str, db: dict) -> date:
    d = db["last_updated"].get(ticker)
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


def fetch_window(symbol: str, time_from: str, time_to: str) -> list[dict]:
    resp = requests.get(
        "https://www.alphavantage.co/query",
        params={
            "function":  "NEWS_SENTIMENT",
            "tickers":   symbol,
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


def collect_ticker(ticker: str, start: date, end: date, seen_urls: set,
                   db: dict, budget: int) -> int:
    """Raccoglie articoli per un ticker, una chiamata per finestra da 30 giorni.
    Scrive in `db` finestra per finestra e avanza `last_updated[ticker]` solo
    dopo ogni finestra completata: se il run si interrompe (rate limit, budget
    finito) il progresso è coerente e riparte senza buchi né sovrapposizioni.
    Non spende più di `budget` chiamate. Restituisce le chiamate effettuate."""
    symbol  = to_av_symbol(ticker)
    windows = build_windows(start, end)
    label   = ticker if symbol == ticker else f"{ticker} (→{symbol})"
    print(f"\n[{label}] {start} → {end} | {len(windows)} finestre | budget {budget}")

    calls_used = 0
    added      = 0
    for i, (w_start, w_end) in enumerate(windows, 1):
        if calls_used >= budget:
            print(f"  budget del run esaurito — finestre rimanenti al prossimo run")
            break
        time.sleep(1.2)  # rispetta il rate limit (~1 req/s)
        tf = w_start.strftime("%Y%m%d") + "T0000"
        tt = w_end.strftime("%Y%m%d")   + "T2359"
        print(f"  [{i}/{len(windows)}] {w_start} → {w_end}", end="", flush=True)
        try:
            feed = fetch_window(symbol, tf, tt)
        except RateLimit:
            raise  # propaga: l'intero run si ferma e salva (gestito in main)
        except APIError as e:
            # "Error Message" = ticker non valido o richiesta rifiutata. Non
            # blocchiamo il ticker per sempre: trattiamo come 0 articoli e
            # avanziamo comunque il checkpoint oltre questa finestra.
            print(f"  ⚠ API error: {e}")
            feed = []
        calls_used += 1
        print(f"  {len(feed)} art")

        for item in feed:
            url   = item.get("url", "")
            title = item.get("title", "").strip()
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            db["articles"].append(parse_article(item))
            added += 1

        if len(feed) >= FEED_CAP:
            # Feed troncato: avanziamo solo fino all'ultima data trovata nel feed,
            # così il prossimo run riparte da lì senza saltare gli articoli intermedi.
            raw_last = max(item.get("time_published", "19700101") for item in feed)
            checkpoint = date(int(raw_last[:4]), int(raw_last[4:6]), int(raw_last[6:8]))
            print(f"  ⚠ cap {FEED_CAP} raggiunto — checkpoint a {checkpoint} (non a {w_end})")
        else:
            checkpoint = w_end

        # Finestra completata: avanza il checkpoint del ticker.
        db["last_updated"][ticker] = checkpoint.isoformat()

    print(f"  → {added} nuovi articoli, {calls_used} call")
    return calls_used


# --- main ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", help="Singolo ticker (override rotazione)")
    parser.add_argument("--from",  dest="from_date", help="Data inizio backfill (YYYY-MM-DD)")
    args = parser.parse_args()

    if not AV_KEY:
        print("ERRORE: ALPHAVANTAGE_KEY non trovata in .env o environment")
        sys.exit(1)

    config  = load_tickers_config()
    tickers = [args.ticker] if args.ticker else all_tickers(config)
    end     = date.today() - timedelta(days=1)

    db        = load_news_db()
    seen_urls = {a["url"] for a in db["articles"]}

    # Ordina per staleness — i ticker più indietro vengono processati per primi,
    # così su run successivi il backlog si svuota a rotazione.
    if not args.ticker:
        tickers = sorted(tickers, key=lambda t: last_collected_date(t, db))

    calls_used = 0
    for ticker in tickers:
        if calls_used >= API_CALLS_MAX:
            print(f"\nBudget esaurito ({API_CALLS_MAX} call). Ticker rimanenti rinviati al prossimo run.")
            break

        start = date.fromisoformat(args.from_date) if args.from_date else last_collected_date(ticker, db) + timedelta(days=1)

        if start > end:
            print(f"\n[{ticker}] già aggiornato, skip.")
            continue

        try:
            calls_used += collect_ticker(ticker, start, end, seen_urls, db, API_CALLS_MAX - calls_used)
        except RateLimit as e:
            # collect_ticker ha già committato in `db` le finestre completate:
            # ci fermiamo senza perdere il lavoro fatto finora (salva il finally).
            print(f"\nRATE LIMIT: {e}")
            print("Salvo il progresso e mi fermo — il resto al prossimo run.")
            break
        finally:
            # Persisti dopo ogni ticker così un crash non vanifica il budget speso.
            db["articles"].sort(key=lambda x: x["date"])
            save_news_db(db)

    print(f"\nArticoli totali nel dataset: {len(db['articles'])}")
    print(f"Chiamate API usate: {calls_used}/{API_CALLS_MAX}")


if __name__ == "__main__":
    main()
