"""
split_news_to_gcs.py — migrazione one-shot da news.json globale a file per-ticker

Legge il file locale pipeline/data/news.json e lo smista in file separati su GCS:
  gs://BUCKET/AAPL/news.json
  gs://BUCKET/MSFT/news.json
  ...
  gs://BUCKET/news_checkpoint.json

Ogni articolo viene copiato in tutti i ticker che cita (campo `all_tickers`).

Usage:
    cd pipeline
    python split_news_to_gcs.py            # migra
    python split_news_to_gcs.py --dry-run  # mostra cosa farebbe senza fare nulla
"""

import argparse
import json
from collections import defaultdict  # dizionario con valore di default automatico

from utils import (
    BUCKET_NAME, NEWS_BLOB, NEWS_CHECKPOINT_BLOB,
    all_tickers, load_tickers_config,
    gcs_download_json, gcs_upload_json, news_blob,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostra cosa verrebbe fatto senza eseguire upload")
    args = parser.parse_args()

    print(f"Bucket: gs://{BUCKET_NAME}\n")

    # --- 1. Scarica il database aggiornato da GCS ---

    print(f"Download gs://{BUCKET_NAME}/{NEWS_BLOB} ...")
    db = gcs_download_json(NEWS_BLOB)
    if not db:
        print("ERRORE: news.json non trovato su GCS. Niente da migrare.")
        return

    articles     = db.get("articles", [])
    last_updated = db.get("last_updated", {})  # {"AAPL": "2026-06-06", ...}
    print(f"  {len(articles)} articoli, {len(last_updated)} ticker nel checkpoint\n")

    # Carica la lista dei ticker che tracciamo (i 51 in tickers.json).
    # Filtrare qui evita di creare migliaia di file per i ticker "di passaggio"
    # che Alpha Vantage include in all_tickers di ogni articolo.
    tracked = set(all_tickers(load_tickers_config()))
    print(f"Ticker tracciati in tickers.json: {len(tracked)}\n")

    # Nel vecchio database, le news di ^GSPC sono taggate come "SPY" in all_tickers
    # (perché la pipeline interrogava Alpha Vantage con il simbolo SPY come proxy).
    # Questa mappa inverte l'alias: quando troviamo "SPY" lo trattiamo come "^GSPC".
    TICKER_REMAP = {"SPY": "^GSPC"}

    # --- 2. Smista gli articoli per ticker ---

    # defaultdict(list) crea automaticamente una lista vuota per le chiavi nuove.
    by_ticker: dict[str, list] = defaultdict(list)
    seen_per_ticker: dict[str, set] = defaultdict(set)

    skipped = 0
    for article in articles:
        url     = article.get("url", "")
        tickers = [t["ticker"] for t in article.get("all_tickers", []) if t.get("ticker")]

        if not tickers:
            skipped += 1
            continue

        for ticker in tickers:
            ticker = TICKER_REMAP.get(ticker, ticker)  # es. "SPY" → "^GSPC"
            if ticker not in tracked:
                continue  # ignora i ticker che non monitoriamo
            if url and url in seen_per_ticker[ticker]:
                continue
            seen_per_ticker[ticker].add(url)
            by_ticker[ticker].append(article)

    print(f"Articoli smistati in {len(by_ticker)} ticker ({skipped} saltati senza ticker)\n")

    # --- 3. Carica i file per-ticker su GCS ---

    total_uploaded = 0
    for ticker in sorted(by_ticker):
        ticker_articles = sorted(by_ticker[ticker], key=lambda x: x["date"])
        blob_name       = news_blob(ticker)  # es. "AAPL/news.json"

        lu = last_updated.get(ticker)
        if not lu and ticker_articles:
            lu = ticker_articles[-1]["date"]

        data = {
            "ticker":       ticker,
            "last_updated": lu,
            "articles":     ticker_articles,
        }

        size_kb = len(json.dumps(data)) / 1024
        print(
            f"  {'[DRY RUN] ' if args.dry_run else ''}"
            f"gs://{BUCKET_NAME}/{blob_name}  "
            f"({len(ticker_articles)} art, ~{size_kb:.0f} KB)"
        )

        if not args.dry_run:
            gcs_upload_json(blob_name, data)
            total_uploaded += 1

    # --- 4. Carica il checkpoint globale ---

    merged_checkpoint = dict(last_updated)
    for ticker in by_ticker:
        if ticker not in merged_checkpoint and by_ticker[ticker]:
            merged_checkpoint[ticker] = by_ticker[ticker][-1]["date"]

    print(
        f"\n  {'[DRY RUN] ' if args.dry_run else ''}"
        f"gs://{BUCKET_NAME}/{NEWS_CHECKPOINT_BLOB}  "
        f"({len(merged_checkpoint)} ticker)"
    )
    if not args.dry_run:
        gcs_upload_json(NEWS_CHECKPOINT_BLOB, {"last_updated": merged_checkpoint})
        total_uploaded += 1

    if args.dry_run:
        print(f"\n[DRY RUN] Nessun file modificato. Esegui senza --dry-run per procedere.")
    else:
        print(f"\nMigrazione completata: {total_uploaded} blob caricati su GCS.")


if __name__ == "__main__":
    main()
