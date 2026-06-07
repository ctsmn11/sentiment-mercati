"""
utils.py — costanti e funzioni condivise tra gli script della pipeline

Questo file non va eseguito direttamente: viene importato dagli altri script
con `from utils import ...` e mette a disposizione costanti e funzioni comuni.
"""

import json
import os
from datetime import date
from pathlib import Path

# google-cloud-storage è la libreria ufficiale Google per interagire con GCS
from google.cloud import storage

# --- Percorsi del filesystem locale ---

# Path(__file__) restituisce il percorso assoluto di questo file (utils.py).
# .parent risale alla directory che lo contiene (pipeline/).
# Questo approccio funziona ovunque si lanci lo script, senza dipendere
# dalla directory di lavoro corrente.
BASE_DIR = Path(__file__).parent

# Cartella locale usata solo da script di analisi (EDA) in locale.
# La pipeline in produzione (GitHub Actions) legge e scrive solo su GCS.
DATA_DIR = BASE_DIR / "data"

# Percorso assoluto al file di configurazione dei ticker (sempre nel repo).
TICKERS_F = BASE_DIR / "tickers.json"

# Percorso locale di news.json — usato solo dall'EDA notebook in locale.
NEWS_F = DATA_DIR / "news.json"

# Data di inizio del dataset: non raccogliamo dati prima di questa data.
# Impostata a 2026-01-01 per partire da uno storico pulito e uniforme.
DATASET_START = date(2026, 1, 1)

# --- Configurazione Google Cloud Storage ---

# os.getenv legge una variabile d'ambiente; il secondo argomento è il valore
# di default usato se la variabile non è impostata.
# Su GitHub Actions la variabile GCS_BUCKET è definita nei secrets del repo.
BUCKET_NAME = os.getenv("GCS_BUCKET", "sentiment-mercati-data")

# --- Nomi dei "blob" (file) dentro il bucket GCS ---

# File globale legacy — usato solo dallo script di migrazione split_news_to_gcs.py.
NEWS_BLOB = "news.json"

# Checkpoint globale: solo le date di ultima raccolta per ogni ticker (~2 KB).
# Scaricato una volta per run per ordinare i ticker per staleness.
NEWS_CHECKPOINT_BLOB = "news_checkpoint.json"


def news_blob(ticker: str) -> str:
    """Restituisce il nome del blob GCS per le news di un dato ticker.

    Esempio: news_blob("AAPL") → "AAPL/news.json"
    Stessa convenzione di prices_blob: cartelle virtuali nel bucket.
    """
    return f"{ticker}/news.json"


def prices_blob(ticker: str) -> str:
    """Restituisce il nome del blob GCS per i prezzi di un dato ticker.

    Esempio: prices_blob("AAPL") → "AAPL/prices.json"
    La convenzione 'ticker/prices.json' organizza i file come cartelle virtuali
    dentro il bucket (GCS usa il '/' nel nome come separatore visivo).
    """
    return f"{ticker}/prices.json"


# --- Funzioni helper per Google Cloud Storage ---

def gcs_download_json(blob_name: str, default=None) -> dict:
    """Scarica un file JSON dal bucket GCS e lo restituisce come dizionario Python.

    Se il file non esiste ancora nel bucket (prima esecuzione), restituisce
    il valore `default` invece di andare in errore.

    Parametri:
        blob_name: nome del blob nel bucket, es. "news.json" o "AAPL/prices.json"
        default:   valore restituito se il blob non esiste (None → {})
    """
    # storage.Client() crea una connessione a GCS usando le credenziali
    # dell'ambiente (Application Default Credentials).
    # Su GitHub Actions le credenziali vengono iniettate automaticamente
    # dal secret GOOGLE_APPLICATION_CREDENTIALS.
    client = storage.Client()

    # client.bucket() referenzia il bucket (senza ancora fare chiamate di rete).
    # .blob() referenzia il singolo file dentro il bucket.
    blob = client.bucket(BUCKET_NAME).blob(blob_name)

    # blob.exists() fa una chiamata HTTP al bucket per verificare se il file c'è.
    if not blob.exists():
        return default if default is not None else {}

    # download_as_text() scarica il contenuto del blob come stringa di testo.
    # encoding="utf-8" garantisce che i caratteri non-ASCII (accenti, simboli)
    # vengano decodificati correttamente.
    # json.loads() converte la stringa JSON in un dizionario Python.
    return json.loads(blob.download_as_text(encoding="utf-8"))


def gcs_upload_json(blob_name: str, data):
    """Serializza `data` come JSON e lo carica nel bucket GCS.

    L'upload su GCS è atomico per natura: il blob precedente rimane leggibile
    fino al completamento dell'operazione. Non c'è rischio di file "a metà"
    come con le scritture su disco locale.

    Parametri:
        blob_name: nome del blob di destinazione nel bucket
        data:      dizionario (o lista) Python da serializzare e caricare
    """
    client = storage.Client()
    blob = client.bucket(BUCKET_NAME).blob(blob_name)

    # json.dumps() converte il dizionario Python in una stringa JSON.
    #   ensure_ascii=False → preserva i caratteri non-ASCII (es. lettere accentate)
    #                        invece di convertirli in sequenze \uXXXX.
    #   indent=2           → produce un JSON "leggibile" con indentazione a 2 spazi.
    #                        Più pesante da caricare, ma utile per il debug.
    blob.upload_from_string(
        json.dumps(data, ensure_ascii=False, indent=2),
        content_type="application/json",  # dice a GCS il tipo di contenuto del file
    )


# --- Funzioni di supporto condivise ---

def load_tickers_config() -> dict:
    """Carica e restituisce il contenuto di tickers.json come dizionario.

    tickers.json contiene la lista dei 50 ticker S&P 500 da monitorare
    più il market index (^GSPC) e il numero di versione del dataset.
    """
    # open() apre il file in lettura ('r' è il default).
    # encoding="utf-8" evita errori su Windows dove il default è cp1252.
    # Il blocco 'with' chiude automaticamente il file quando si esce dal blocco.
    with open(TICKERS_F, encoding="utf-8") as f:
        return json.load(f)


def all_tickers(config: dict) -> list[str]:
    """Restituisce la lista completa di ticker da processare: costituenti + indice.

    Esempio di output: ["AAPL", "MSFT", ..., "^GSPC"]
    Il '+' tra due liste Python le concatena in una lista unica.
    """
    return config["tickers"] + [config["market_index"]]


def ticker_data_dir(ticker: str) -> Path:
    """Restituisce il percorso locale alla cartella dati di un ticker.

    Usata solo in locale; in produzione i dati sono su GCS.
    """
    return DATA_DIR / ticker


def bump_version(version: str) -> str:
    """Incrementa il numero di versione del dataset (formato semver: MAJOR.MINOR.PATCH).

    Esempio: "1.2.3" → "1.3.0"
    Il numero minore aumenta di 1 e il patch viene azzerato.
    """
    # split(".") divide la stringa "1.2.3" in una lista ["1", "2", "3"].
    major, minor, patch = version.split(".")
    # int() converte la stringa in numero intero per poter fare l'aritmetica.
    # f"..." è una f-string: le espressioni dentro {} vengono valutate e inserite.
    return f"{major}.{int(minor) + 1}.0"
