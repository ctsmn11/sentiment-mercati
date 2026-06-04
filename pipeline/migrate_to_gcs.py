"""
migrate_to_gcs.py — carica il dataset locale su Google Cloud Storage (one-shot)

Carica:
  - pipeline/data/news.json          → gs://BUCKET/news.json
  - pipeline/data/{ticker}/prices.json → gs://BUCKET/{ticker}/prices.json

Rinomina automaticamente BRK.B → BRK-B (il ticker corretto per Alpha Vantage).

Usage:
    cd pipeline
    python migrate_to_gcs.py [--dry-run]
"""

import argparse
import json
from pathlib import Path

from google.cloud import storage

from utils import BUCKET_NAME, DATA_DIR, NEWS_F

TICKER_RENAMES = {
    "BRK.B": "BRK-B",
}


def upload_blob(bucket, src_path: Path, blob_name: str, dry_run: bool):
    size_kb = src_path.stat().st_size / 1024
    print(f"  {'[DRY RUN] ' if dry_run else ''}upload {src_path.relative_to(DATA_DIR.parent)} → gs://{BUCKET_NAME}/{blob_name}  ({size_kb:.0f} KB)")
    if not dry_run:
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(src_path), content_type="application/json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Mostra cosa verrebbe caricato senza farlo davvero")
    args = parser.parse_args()

    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)

    print(f"Bucket: gs://{BUCKET_NAME}\n")

    # 1. news.json
    if NEWS_F.exists():
        upload_blob(bucket, NEWS_F, "news.json", args.dry_run)
    else:
        print(f"  ⚠ {NEWS_F} non trovato, skip.")

    # 2. prezzi per ticker
    print()
    price_files = sorted(DATA_DIR.glob("*/prices.json"))
    for p in price_files:
        ticker = p.parent.name
        blob_name = f"{TICKER_RENAMES.get(ticker, ticker)}/prices.json"
        upload_blob(bucket, p, blob_name, args.dry_run)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}{'Fatto' if not args.dry_run else 'Nessun file caricato'}. {1 + len(price_files)} blob totali.")


if __name__ == "__main__":
    main()
