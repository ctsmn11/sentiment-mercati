"""
fetch_prices.py — scarica prezzi storici per tutti i ticker in tickers.json

Aggiorna data/{ticker}/prices.json con i giorni mancanti.
Ogni record include: date, close, return (rendimento rispetto al giorno precedente).

Usage:
    python fetch_prices.py              # tutti i ticker
    python fetch_prices.py --ticker AAPL
"""

import argparse
import json
import sys
from datetime import date, timedelta

import yfinance as yf  # libreria non ufficiale che scarica dati storici da Yahoo Finance

from utils import (
    DATASET_START, all_tickers, load_tickers_config,
    prices_blob, gcs_download_json, gcs_upload_json,
)

# Su Windows la console di default è cp1252 e va in errore sui caratteri non
# ASCII (→, ⚠) usati nei log; su CI lo stdout è già UTF-8 e questo è un no-op.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Mappa ticker → simbolo Yahoo Finance, per i casi in cui divergono.
# ^VWCE è il ticker Bloomberg/standard; Yahoo lo conosce come VWCE.AS (borsa di Amsterdam).
TICKER_ALIASES = {
    "^VWCE": "VWCE.AS",
}


def to_yfinance_symbol(ticker: str) -> str:
    """Converte un ticker nel simbolo atteso da Yahoo Finance.

    Yahoo usa il trattino al posto del punto per le classi di azioni
    (es. BRK.B → BRK-B); gli alias espliciti hanno la precedenza.

    Il replace(".BrK.B") avviene DOPO il controllo degli alias, quindi gli alias
    non vengono accidentalmente alterati dalla sostituzione del punto.
    """
    if ticker in TICKER_ALIASES:
        return TICKER_ALIASES[ticker]
    # Yahoo Finance usa "-" invece di "." nelle classi di azioni:
    # "BRK.B" (Berkshire Hathaway classe B) → "BRK-B"
    return ticker.replace(".", "-")


# --- Funzioni di I/O (lettura/scrittura prezzi su GCS) ---

def load_existing_prices(ticker: str) -> list[dict]:
    """Carica i prezzi già salvati per un ticker da GCS.

    Restituisce una lista di record [{"date": "2026-01-02", "close": 123.45}, ...].
    Se il file non esiste ancora (primo avvio), restituisce una lista vuota.
    """
    # gcs_download_json restituisce {} se il blob non esiste.
    # .get("prices", []) restituisce la lista dei prezzi o [] se la chiave manca.
    data = gcs_download_json(prices_blob(ticker), default={})
    return data.get("prices", [])


def save_prices(ticker: str, prices: list[dict], version: str):
    """Salva la lista dei prezzi su GCS nel formato atteso dalla dashboard.

    Il file include anche il ticker e la versione del dataset per tracciabilità.
    I ritorni giornalieri vengono calcolati da with_returns() prima del salvataggio.
    """
    out = {
        "ticker":          ticker,
        "dataset_version": version,  # versione da tickers.json (es. "1.3.0")
        "prices":          with_returns(prices),  # aggiunge il campo "return" a ogni record
    }
    gcs_upload_json(prices_blob(ticker), out)


# --- Funzioni di calcolo ---

def with_returns(prices: list[dict]) -> list[dict]:
    """Aggiunge il rendimento giornaliero a ogni record di prezzo.

    Il rendimento (return) è la variazione percentuale rispetto al giorno precedente:
        return = (prezzo_oggi - prezzo_ieri) / prezzo_ieri

    Il primo giorno ha return=None perché non c'è un giorno precedente.
    Esempio: close ieri = 100, close oggi = 102 → return = 0.02 (2%)

    Il parametro `prices` è la lista originale; la funzione non la modifica,
    ma costruisce e restituisce una lista nuova con il campo "return" aggiunto.
    """
    result = []
    for i, p in enumerate(prices):
        ret = None
        if i > 0:  # non calcolare il ritorno per il primo giorno (i=0)
            prev = prices[i - 1]["close"]
            # Dividiamo solo se prev è diverso da 0, per evitare ZeroDivisionError.
            ret  = round((p["close"] - prev) / prev, 6) if prev else None
        # {**p, "return": ret} crea un nuovo dizionario copiando tutti i campi di p
        # e aggiungendo/sovrascrivendo "return". È equivalente a:
        #   new_p = p.copy()
        #   new_p["return"] = ret
        result.append({**p, "return": ret})
    return result


def dedup_by_date(prices: list[dict]) -> list[dict]:
    """Rimuove i record duplicati per la stessa data, ordinando per data crescente.

    Un duplicato può comparire se fetch_from_yfinance scarica un periodo che si
    sovrappone ai dati già esistenti. Questo non dovrebbe succedere in condizioni
    normali, ma la funzione difende da eventuali anomalie.

    Restituisce solo i campi "date" e "close" (elimina eventualmente "return" dai
    dati già salvati — verrà ricalcolato da with_returns() al momento del salvataggio).
    """
    seen: set[str] = set()  # set delle date già viste, per dedup rapido
    result = []
    # sorted() ordina la lista per data (le stringhe ISO "YYYY-MM-DD" si ordinano
    # correttamente come stringhe normali grazie al formato zero-padded).
    for p in sorted(prices, key=lambda x: x["date"]):
        if p["date"] not in seen:
            seen.add(p["date"])
            result.append({"date": p["date"], "close": p["close"]})
    return result


# --- Funzione che chiama Yahoo Finance ---

def fetch_from_yfinance(ticker: str, start: date, end: date) -> list[dict]:
    """Scarica i prezzi di chiusura giornalieri da Yahoo Finance.

    Parametri:
        ticker: simbolo standard (es. "AAPL", "^GSPC")
        start:  primo giorno da scaricare
        end:    ultimo giorno da scaricare (incluso)

    Restituisce una lista di dizionari [{"date": "YYYY-MM-DD", "close": 123.45}, ...].
    Restituisce [] se Yahoo non ha dati per il periodo richiesto.
    """
    yf_ticker = to_yfinance_symbol(ticker)

    # Yahoo Finance usa un intervallo "half-open": [start, end).
    # Per includere `end`, dobbiamo passare end+1 come data di fine.
    end_excl = (end + timedelta(days=1)).isoformat()

    # yf.Ticker(symbol).history() scarica i dati OHLCV (Open/High/Low/Close/Volume).
    # auto_adjust=True applica automaticamente le correzioni per split azionari
    # e distribuzione di dividendi, così i prezzi storici sono comparabili.
    df = yf.Ticker(yf_ticker).history(start=start.isoformat(), end=end_excl, auto_adjust=True)

    if df.empty:
        return []  # nessun dato per il periodo richiesto (es. weekend, festivi)

    # df è un DataFrame pandas. df.iterrows() itera su ogni riga restituendo
    # la coppia (indice, serie di valori). L'indice è un timestamp pandas.
    # idx.date() converte il timestamp in un oggetto date Python.
    return [
        {"date": str(idx.date()), "close": round(float(row["Close"]), 4)}
        for idx, row in df.iterrows()
    ]


# --- main: punto di ingresso dello script ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", help="Singolo ticker (override)")
    args = parser.parse_args()

    config  = load_tickers_config()
    version = config["version"]  # versione del dataset da tickers.json
    end     = date.today() - timedelta(days=1)  # ieri: la giornata di oggi è incompleta

    tickers_to_run = [args.ticker] if args.ticker else all_tickers(config)

    attempted = 0  # numero di ticker che avevano giorni da scaricare
    failed    = []  # lista dei ticker falliti (per il report finale)

    for ticker in tickers_to_run:
        # Carica i prezzi già salvati per questo ticker da GCS.
        existing = load_existing_prices(ticker)

        # Determina il primo giorno da scaricare:
        # - se ci sono già dati, parte dal giorno dopo l'ultimo dato salvato
        # - se non ci sono dati (lista vuota), parte da DATASET_START
        start = (
            date.fromisoformat(existing[-1]["date"]) + timedelta(days=1)
            if existing
            else DATASET_START
        )

        if start > end:
            # Il ticker è già aggiornato a ieri: niente da fare.
            print(f"[{ticker}] prezzi già aggiornati, skip.")
            continue

        attempted += 1
        print(f"[{ticker}] {start} → {end}...", end=" ", flush=True)

        # Un fallimento di yfinance su un ticker (rete, throttling Yahoo) non
        # deve abortire l'intero run: isola l'errore e prosegue con gli altri.
        # `except Exception` cattura qualsiasi tipo di eccezione.
        try:
            new_prices = fetch_from_yfinance(ticker, start, end)
        except Exception as e:
            print(f"ERRORE: {e}")
            failed.append(ticker)
            continue  # salta al ticker successivo

        if not new_prices:
            print("nessun dato.")
            continue

        # Unisci i dati vecchi e nuovi, rimuovi eventuali duplicati e ordina per data.
        all_prices = dedup_by_date(existing + new_prices)
        save_prices(ticker, all_prices, version)
        print(f"{len(new_prices)} giorni aggiunti (totale: {len(all_prices)})")

    # Report finale dei fallimenti.
    if failed:
        print(f"\n⚠ {len(failed)}/{attempted} ticker falliti: {', '.join(failed)}")

    # Esce con errore (sys.exit(1)) solo se TUTTI i ticker sono falliti,
    # il che indica un problema sistemico (es. Yahoo Finance down).
    # I fallimenti parziali lasciano comunque dati validi → non bloccare il commit.
    if attempted and len(failed) == attempted:
        print("Tutti i ticker hanno fallito — esco con errore.")
        sys.exit(1)


if __name__ == "__main__":
    main()
