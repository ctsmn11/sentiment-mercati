"""
update_tickers.py — confronta la lista ticker corrente con i top 50 SPDR

Eseguire manualmente ogni trimestre per rilevare cambi nella composizione.
Se ci sono differenze, aggiornare tickers.json a mano e fare bump della versione.

Usage:
    python update_tickers.py
"""

import io
import json
import sys
from datetime import date

import pandas as pd
import requests

from utils import TICKERS_F, bump_version, load_tickers_config

SPY_HOLDINGS_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
TOP_N        = 50
SKIP_TICKERS = {"GOOG"}  # Alphabet ha due classi — teniamo solo GOOGL


# --- funzioni ---

def fetch_ranked_tickers(n: int) -> list[str]:
    resp = requests.get(SPY_HOLDINGS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content), skiprows=4)
    df = df.dropna(subset=["Ticker"])
    df = df[df["Ticker"] != "Ticker"]
    result: list[str] = []
    for ticker in df["Ticker"].tolist():
        if ticker in SKIP_TICKERS:
            continue
        if ticker not in result:
            result.append(ticker)
        if len(result) == n:
            break
    return result


def apply_update(current: dict, new_tickers: list[str]):
    updated = {
        "version":      bump_version(current["version"]),
        "updated":      date.today().isoformat(),
        "market_index": current["market_index"],
        "tickers":      new_tickers,
    }
    with open(TICKERS_F, "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False, indent=2)
    print(f"tickers.json aggiornato a versione {updated['version']}.")


# --- main ---

def main():
    print("Scarico holdings SPY da SPDR...")
    try:
        spdr_top50 = fetch_ranked_tickers(TOP_N)
    except Exception as e:
        print(f"ERRORE fetch SPDR: {e}")
        sys.exit(1)

    current = load_tickers_config()
    entered = sorted(set(spdr_top50) - set(current["tickers"]))
    exited  = sorted(set(current["tickers"]) - set(spdr_top50))

    if not entered and not exited:
        print("Nessuna variazione — tickers.json è aggiornato.")
        return

    print(f"\nVariazioni rilevate:")
    print(f"  Entrano : {entered}")
    print(f"  Escono  : {exited}")
    print(f"\nAggiornare tickers.json? [y/N] ", end="")
    if input().strip().lower() == "y":
        apply_update(current, spdr_top50)
    else:
        print("Nessuna modifica.")


if __name__ == "__main__":
    main()
