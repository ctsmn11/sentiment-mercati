"""
migrate_to_gcs.py — carica il dataset locale su Google Cloud Storage (one-shot)

Questo script è stato usato UNA SOLA VOLTA per la migrazione iniziale dei dati
dal repository git a GCS. Non fa parte della pipeline giornaliera.

Carica:
  - pipeline/data/news.json          → gs://BUCKET/news.json
  - pipeline/data/{ticker}/prices.json → gs://BUCKET/{ticker}/prices.json

Rinomina automaticamente BRK.B → BRK-B (il ticker corretto per Alpha Vantage).

Usage:
    cd pipeline
    python migrate_to_gcs.py [--dry-run]
"""

import argparse
from pathlib import Path

# google-cloud-storage è la libreria ufficiale Google per interagire con GCS
from google.cloud import storage

# Importa costanti e percorsi definiti in utils.py
from utils import BUCKET_NAME, DATA_DIR, NEWS_F

# Mappa dei ticker da rinominare durante la migrazione.
# BRK.B (con il punto) era il nome usato nelle cartelle locali,
# ma Alpha Vantage e la pipeline producono BRK-B (con il trattino).
# La migrazione corregge questo al volo.
TICKER_RENAMES = {
    "BRK.B": "BRK-B",
}


def upload_blob(bucket, src_path: Path, blob_name: str, dry_run: bool):
    """Carica un file locale come blob nel bucket GCS.

    Se dry_run=True, stampa solo cosa verrebbe caricato senza fare nulla.
    Questo pattern è comune negli script di migrazione per verificare
    l'operazione prima di eseguirla davvero.

    Parametri:
        bucket:    oggetto bucket GCS (dalla libreria google-cloud-storage)
        src_path:  percorso assoluto del file locale da caricare
        blob_name: nome del blob di destinazione nel bucket (es. "AAPL/prices.json")
        dry_run:   se True, simula senza caricare
    """
    # stat() restituisce le informazioni del file (dimensione, data modifica, ecc.).
    # st_size è la dimensione in byte; dividiamo per 1024 per convertire in KB.
    size_kb = src_path.stat().st_size / 1024

    # relative_to() restituisce il percorso relativo di src_path rispetto a DATA_DIR.parent,
    # così nel log vediamo "data/news.json" invece del percorso assoluto completo.
    print(
        f"  {'[DRY RUN] ' if dry_run else ''}"
        f"upload {src_path.relative_to(DATA_DIR.parent)} "
        f"→ gs://{BUCKET_NAME}/{blob_name}  ({size_kb:.0f} KB)"
    )

    if not dry_run:
        # bucket.blob() crea un riferimento al blob (non fa ancora chiamate di rete).
        blob = bucket.blob(blob_name)
        # upload_from_filename() legge il file dal disco e lo carica su GCS.
        # content_type dice a GCS di che tipo è il file (utile per browser/API).
        blob.upload_from_filename(str(src_path), content_type="application/json")


def main():
    # Configura l'argomento --dry-run.
    # action="store_true" significa: se --dry-run è presente, args.dry_run = True;
    # se assente, args.dry_run = False. Non richiede un valore aggiuntivo.
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra cosa verrebbe caricato senza farlo davvero",
    )
    args = parser.parse_args()

    # storage.Client() si connette a GCS usando le credenziali dell'ambiente
    # (Application Default Credentials: GOOGLE_APPLICATION_CREDENTIALS o gcloud auth).
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)  # referenzia il bucket (senza chiamate di rete)

    print(f"Bucket: gs://{BUCKET_NAME}\n")

    # --- 1. Carica news.json ---
    # NEWS_F.exists() verifica se il file esiste nel filesystem locale.
    if NEWS_F.exists():
        upload_blob(bucket, NEWS_F, "news.json", args.dry_run)
    else:
        print(f"  ⚠ {NEWS_F} non trovato, skip.")

    print()  # riga vuota per separare i due blocchi nel log

    # --- 2. Carica i file dei prezzi per ogni ticker ---
    # DATA_DIR.glob("*/prices.json") cerca tutti i file prices.json dentro
    # le sottocartelle di DATA_DIR, es.:
    #   data/AAPL/prices.json
    #   data/MSFT/prices.json
    #   data/BRK.B/prices.json
    # sorted() li ordina alfabeticamente per un output più leggibile.
    price_files = sorted(DATA_DIR.glob("*/prices.json"))

    for p in price_files:
        # p.parent.name è il nome della cartella contenitore = il ticker.
        # Esempio: per "data/AAPL/prices.json", p.parent.name = "AAPL"
        ticker = p.parent.name

        # Applica l'eventuale rinomina (es. "BRK.B" → "BRK-B").
        # dict.get(key, default) restituisce il valore se la chiave esiste,
        # altrimenti il ticker originale invariato.
        blob_name = f"{TICKER_RENAMES.get(ticker, ticker)}/prices.json"
        upload_blob(bucket, p, blob_name, args.dry_run)

    # Report finale: 1 (news.json) + numero di file prezzi = totale blob caricati.
    total = 1 + len(price_files)
    print(
        f"\n{'[DRY RUN] ' if args.dry_run else ''}"
        f"{'Fatto' if not args.dry_run else 'Nessun file caricato'}. {total} blob totali."
    )


# Esegui main() solo se lo script viene avviato direttamente, non se importato.
if __name__ == "__main__":
    main()
