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
from datetime import date, timedelta

import yfinance as yf

from utils import DATASET_START, all_tickers, load_tickers_config, ticker_data_dir

TICKER_ALIASES = {
    "^VWCE": "VWCE.AS",
}


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
    with open(d / "prices.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


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
    yf_ticker = TICKER_ALIASES.get(ticker, ticker)
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

    for ticker in tickers_to_run:
        existing = load_existing_prices(ticker)
        start    = date.fromisoformat(max(p["date"] for p in existing)) + timedelta(days=1) if existing else DATASET_START

        if start > end:
            print(f"[{ticker}] prezzi già aggiornati, skip.")
            continue

        print(f"[{ticker}] {start} → {end}...", end=" ", flush=True)
        new_prices = fetch_from_yfinance(ticker, start, end)

        if not new_prices:
            print("nessun dato.")
            continue

        all_prices = dedup_by_date(existing + new_prices)
        save_prices(ticker, all_prices, version)
        print(f"{len(new_prices)} giorni aggiunti (totale: {len(all_prices)})")


if __name__ == "__main__":
    main()
