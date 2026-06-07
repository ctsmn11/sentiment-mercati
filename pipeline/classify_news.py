"""
classify_news.py — raccoglie notizie + sentiment da Alpha Vantage per ticker

Per ogni ticker in tickers.json (costituenti + indice di mercato):
  - Scarica articoli dal giorno successivo all'ultima raccolta fino a ieri,
    in finestre da 30 giorni, UNA chiamata API per finestra.
  - Dedup per URL locale al ticker: evita duplicati dentro lo stesso file,
    ma un articolo che cita più ticker viene salvato in ognuno dei file.
    La dedup cross-ticker avviene a valle, al momento dell'analisi.
  - Avanza il checkpoint `last_updated` dopo OGNI finestra completata,
    così ogni chiamata produce progresso permanente: niente lavoro perso e
    niente deadlock se il budget giornaliero (25 call) finisce a metà.

Struttura dei file su GCS:
  news_checkpoint.json  →  {"last_updated": {"AAPL": "2026-06-06", ...}}
  AAPL/news.json        →  {"ticker": "AAPL", "last_updated": "2026-06-06", "articles": [...]}
  MSFT/news.json        →  {"ticker": "MSFT", "last_updated": "2026-06-05", "articles": [...]}
  ...

Usage:
    python classify_news.py                      # tutti i ticker, a rotazione
    python classify_news.py --ticker AAPL        # singolo ticker
    python classify_news.py --from 2026-01-01    # backfill da data specifica
"""

import argparse
import os
import sys
import time
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

from utils import (
    DATASET_START, NEWS_CHECKPOINT_BLOB, all_tickers, load_tickers_config,
    gcs_download_json, gcs_upload_json, news_blob,
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
FEED_CAP      = 1000

# ^GSPC non è supportato da Alpha Vantage come ticker di news: usiamo SPY (l'ETF
# che replica l'indice) come proxy del "sentiment di mercato".
NEWS_TICKER_ALIASES = {"^GSPC": "SPY"}


class APIError(Exception):
    """Errore generico restituito dall'API Alpha Vantage."""


class RateLimit(APIError):
    """Budget giornaliero esaurito — fermarsi e salvare il progresso."""


def to_av_symbol(ticker: str) -> str:
    """Converte il ticker nel simbolo corrispondente su Alpha Vantage."""
    return NEWS_TICKER_ALIASES.get(ticker, ticker)


# --- Funzioni di I/O per-ticker ---

def load_ticker_data(ticker: str) -> dict:
    """Carica il file news di un singolo ticker da GCS.

    Struttura restituita:
        {"ticker": "AAPL", "last_updated": "2026-06-06", "articles": [...]}

    Se il file non esiste ancora (ticker mai processato), restituisce la
    struttura vuota con last_updated=None, così il ciclo di raccolta parte
    da DATASET_START.
    """
    data = gcs_download_json(news_blob(ticker), default={})
    # setdefault aggiunge la chiave solo se mancante: sicuro anche su file parziali
    data.setdefault("ticker", ticker)
    data.setdefault("last_updated", None)
    data.setdefault("articles", [])
    return data


def save_ticker_data(ticker: str, data: dict):
    """Salva il file news di un singolo ticker su GCS.

    Upload GCS è atomico: il file precedente resta leggibile fino al
    completamento — nessun rischio di file corrotti.
    """
    gcs_upload_json(news_blob(ticker), data)


def load_checkpoint() -> dict:
    """Carica il checkpoint globale da GCS (~2 KB).

    Il checkpoint contiene solo le date di ultima raccolta per ogni ticker,
    usate per ordinare i ticker per staleness all'inizio del run.
    """
    cp = gcs_download_json(NEWS_CHECKPOINT_BLOB, default={})
    cp.setdefault("last_updated", {})
    return cp


def save_checkpoint(checkpoint: dict):
    """Salva il checkpoint globale su GCS."""
    gcs_upload_json(NEWS_CHECKPOINT_BLOB, checkpoint)


def last_collected_date(ticker: str, checkpoint: dict) -> date:
    """Restituisce la data dell'ultima raccolta per un ticker dal checkpoint.

    Se il ticker non è mai stato processato, restituisce DATASET_START - 1 giorno
    così il primo ciclo parte esattamente da DATASET_START.
    """
    d = checkpoint["last_updated"].get(ticker)
    return date.fromisoformat(d) if d else DATASET_START - timedelta(days=1)


# --- Funzioni API ---

def to_float(x, default: float = 0.0) -> float:
    """Converte in float tollerando None e stringhe vuote/non numeriche."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def av_label(score: float) -> str:
    """Converte uno score numerico nell'etichetta testuale corrispondente."""
    if score > 0.1:
        return "positive"
    if score < -0.1:
        return "negative"
    return "neutral"


def fetch_window(symbol: str, time_from: str, time_to: str) -> list[dict]:
    """Chiama l'API Alpha Vantage NEWS_SENTIMENT e restituisce la lista degli articoli.

    Solleva RateLimit se il budget è esaurito, APIError per altri errori.
    """
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
    # Alpha Vantage segnala il throttling con "Information" o "Note" nel body,
    # non con un codice HTTP di errore — dobbiamo rilevarlo manualmente.
    if "Information" in data or "Note" in data:
        raise RateLimit(data.get("Information") or data.get("Note"))
    if "Error Message" in data:
        raise APIError(data["Error Message"])
    return data.get("feed", [])


# --- Logica di raccolta ---

def build_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Divide l'intervallo [start, end] in finestre da WINDOW_DAYS giorni."""
    windows = []
    cur = start
    while cur <= end:
        w_end = min(cur + timedelta(days=WINDOW_DAYS - 1), end)
        windows.append((cur, w_end))
        cur = w_end + timedelta(days=1)
    return windows


def parse_article(item: dict) -> dict:
    """Estrae e normalizza i campi rilevanti da un articolo dell'API."""
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
        # Manteniamo il sentiment per tutti i ticker citati nell'articolo:
        # utile per l'analisi cross-ticker e per la dedup a valle.
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
                   data: dict, budget: int) -> int:
    """Raccoglie articoli per un ticker, una chiamata per finestra da 30 giorni.

    Aggiorna `data` (il dizionario per-ticker) in-place finestra per finestra:
    - data["articles"] viene arricchito con i nuovi articoli
    - data["last_updated"] avanza dopo ogni finestra completata

    La dedup è locale al ticker: seen_urls contiene solo gli URL già presenti
    nel file di quel ticker. Articoli che citano più ticker vengono salvati
    in ognuno dei file corrispondenti — la dedup globale avviene all'analisi.

    Restituisce il numero di chiamate API effettuate.
    """
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
            raise  # propaga: l'intero run si ferma (gestito in main)
        except APIError as e:
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
            data["articles"].append(parse_article(item))
            added += 1

        if len(feed) >= FEED_CAP:
            # Feed troncato: il checkpoint avanza solo fino all'ultima data trovata,
            # così il prossimo run riparte da lì senza saltare articoli intermedi.
            raw_last   = max(item.get("time_published", "19700101") for item in feed)
            checkpoint = date(int(raw_last[:4]), int(raw_last[4:6]), int(raw_last[6:8]))
            print(f"  ⚠ cap {FEED_CAP} raggiunto — checkpoint a {checkpoint} (non a {w_end})")
        else:
            checkpoint = w_end

        # Aggiorna il checkpoint nel dizionario per-ticker (stringa ISO "YYYY-MM-DD").
        data["last_updated"] = checkpoint.isoformat()

    print(f"  → {added} nuovi articoli, {calls_used} call")
    return calls_used


# --- main ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker",  help="Singolo ticker (override rotazione)")
    parser.add_argument("--from", dest="from_date", help="Data inizio backfill (YYYY-MM-DD)")
    args = parser.parse_args()

    if not AV_KEY:
        print("ERRORE: ALPHAVANTAGE_KEY non trovata in .env o environment")
        sys.exit(1)

    config  = load_tickers_config()
    tickers = [args.ticker] if args.ticker else all_tickers(config)
    end     = date.today() - timedelta(days=1)

    # Scarica il checkpoint globale una volta sola — è un file minuscolo (~2 KB)
    # che contiene solo le date di ultima raccolta per ogni ticker.
    # Serve per ordinare i ticker per staleness e per aggiornarlo dopo ogni ticker.
    checkpoint = load_checkpoint()

    # Ordina per staleness: i ticker più indietro vengono processati per primi,
    # così su più run il backlog si svuota a rotazione rientrando nel budget.
    if not args.ticker:
        tickers = sorted(tickers, key=lambda t: last_collected_date(t, checkpoint))

    calls_used = 0
    for ticker in tickers:
        if calls_used >= API_CALLS_MAX:
            print(f"\nBudget esaurito ({API_CALLS_MAX} call). Ticker rimanenti rinviati al prossimo run.")
            break

        start = (
            date.fromisoformat(args.from_date)
            if args.from_date
            else last_collected_date(ticker, checkpoint) + timedelta(days=1)
        )

        if start > end:
            print(f"\n[{ticker}] già aggiornato, skip.")
            continue

        # Carica il file news di questo specifico ticker da GCS.
        # È un file piccolo (solo gli articoli di quel ticker) — download veloce.
        ticker_data = load_ticker_data(ticker)
        # Costruisce il set degli URL già presenti nel file di questo ticker.
        # La dedup è locale: non importa se lo stesso URL è in altri file ticker.
        seen_urls = {a["url"] for a in ticker_data["articles"]}

        try:
            calls_used += collect_ticker(
                ticker, start, end, seen_urls, ticker_data,
                API_CALLS_MAX - calls_used,
            )
        except RateLimit as e:
            print(f"\nRATE LIMIT: {e}")
            print("Salvo il progresso e mi fermo — il resto al prossimo run.")
            # Salva il progresso parziale di questo ticker prima di uscire.
            ticker_data["articles"].sort(key=lambda x: x["date"])
            save_ticker_data(ticker, ticker_data)
            checkpoint["last_updated"][ticker] = ticker_data["last_updated"] or ""
            save_checkpoint(checkpoint)
            break
        else:
            # Nessuna eccezione: ticker completato. Salva articoli e checkpoint.
            ticker_data["articles"].sort(key=lambda x: x["date"])
            save_ticker_data(ticker, ticker_data)       # upload piccolo (solo questo ticker)
            checkpoint["last_updated"][ticker] = ticker_data["last_updated"] or ""
            save_checkpoint(checkpoint)                 # upload minuscolo (~2 KB)

    print(f"\nChiamate API usate: {calls_used}/{API_CALLS_MAX}")


if __name__ == "__main__":
    main()
