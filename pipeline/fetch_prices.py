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
import os
import sys
from datetime import date, timedelta

import yfinance as yf

from utils import DATASET_START, all_tickers, load_tickers_config, ticker_data_dir

TICKER_ALIASES = {
    "^VWCE": "VWCE.AS",
}


def to_yfinance_symbol(ticker: str) -> str:
    """Converte un ticker nel simbolo atteso da Yahoo Finance.
    Yahoo usa il trattino al posto del punto per le classi di azioni
    (es. BRK.B → BRK-B); gli alias espliciti hanno la precedenza."""
    if ticker in TICKER_ALIASES:
        return TICKER_ALIASES[ticker]
    return ticker.replace(".", "-")


# --- funzioni di I/O ---

def load_existing_prices(ticker: str) -> list[dict]:
    path = ticker_data_dir(ticker) / "prices.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("prices", [])


def save_prices(ticker: str, prices: list[dict], version: str):
    d = ticker_data_dir(ticker)
    d.mkdir(parents=True, exist_ok=True)
    out = {
        "ticker":          ticker,
        "dataset_version": version,
        "prices":          with_returns(prices),
    }
    # Scrittura atomica: temp + rename, così un'interruzione a metà scrittura
    # non lascia un prices.json troncato (che verrebbe committato dal CI).
    dst = d / "prices.json"
    tmp = dst.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, dst)


# --- funzioni di calcolo ---

def with_returns(prices: list[dict]) -> list[dict]:
    result = []
    for i, p in enumerate(prices):
        ret = None
        if i > 0:
            prev = prices[i - 1]["close"]
            ret  = round((p["close"] - prev) / prev, 6) if prev else None
        result.append({**p, "return": ret})
    return result


def dedup_by_date(prices: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for p in sorted(prices, key=lambda x: x["date"]):
        if p["date"] not in seen:
            seen.add(p["date"])
            result.append({"date": p["date"], "close": p["close"]})
    return result


# --- funzione API ---

def fetch_from_yfinance(ticker: str, start: date, end: date) -> list[dict]:
    yf_ticker = to_yfinance_symbol(ticker)
    end_excl  = (end + timedelta(days=1)).isoformat()
    df = yf.Ticker(yf_ticker).history(start=start.isoformat(), end=end_excl, auto_adjust=True)
    if df.empty:
        return []
    return [
        {"date": str(idx.date()), "close": round(float(row["Close"]), 4)}
        for idx, row in df.iterrows()
    ]


# --- main ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", help="Singolo ticker (override)")
    args = parser.parse_args()

    config  = load_tickers_config()
    version = config["version"]
    end     = date.today() - timedelta(days=1)

    tickers_to_run = [args.ticker] if args.ticker else all_tickers(config)

    attempted = 0  # ticker che avevano giorni da scaricare
    failed    = []
    for ticker in tickers_to_run:
        existing = load_existing_prices(ticker)
        start    = date.fromisoformat(existing[-1]["date"]) + timedelta(days=1) if existing else DATASET_START

        if start > end:
            print(f"[{ticker}] prezzi già aggiornati, skip.")
            continue

        attempted += 1
        print(f"[{ticker}] {start} → {end}...", end=" ", flush=True)
        # Un fallimento di yfinance su un ticker (rete, throttling Yahoo) non
        # deve abortire l'intero run: isoliamo l'errore e proseguiamo.
        try:
            new_prices = fetch_from_yfinance(ticker, start, end)
        except Exception as e:
            print(f"ERRORE: {e}")
            failed.append(ticker)
            continue

        if not new_prices:
            print("nessun dato.")
            continue

        all_prices = dedup_by_date(existing + new_prices)
        save_prices(ticker, all_prices, version)
        print(f"{len(new_prices)} giorni aggiunti (totale: {len(all_prices)})")

    if failed:
        print(f"\n⚠ {len(failed)}/{attempted} ticker falliti: {', '.join(failed)}")
    # Esci con errore solo se TUTTO è fallito (probabile outage sistemico):
    # i fallimenti parziali lasciano comunque dati validi da committare.
    if attempted and len(failed) == attempted:
        print("Tutti i ticker hanno fallito — esco con errore.")
        sys.exit(1)


if __name__ == "__main__":
    main()
