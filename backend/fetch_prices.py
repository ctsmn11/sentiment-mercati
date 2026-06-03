"""
fetch_prices.py — scarica prezzi storici per il ticker configurato

Legge il date range da news_classified.json (se esiste), altrimenti usa gli ultimi 30 giorni.
Output: data/prices.json
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
CLASSIFIED_FILE = DATA_DIR / "news_classified.json"

# Aliases for tickers that are not directly supported by yfinance
TICKER_ALIASES = {
    "^VWCE": "VWCE.AS",  # Vanguard FTSE All-World UCITS ETF on Euronext Amsterdam
}


def main():
    if CLASSIFIED_FILE.exists():
        with open(CLASSIFIED_FILE, encoding="utf-8") as f:
            classified = json.load(f)
        ticker = classified.get("ticker", os.getenv("TICKER", "^GSPC"))
        start = classified.get("start")
        end = classified.get("end")
        if not start or not end:
            articles = classified.get("articles", [])
            if articles:
                start = min(a["date"] for a in articles)
                end = max(a["date"] for a in articles)
            else:
                print("ERRORE: news_classified.json non contiene articoli né date range")
                sys.exit(1)
    else:
        ticker = os.getenv("TICKER", "^GSPC")
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=29)).isoformat()

    end_exclusive = (date.fromisoformat(end) + timedelta(days=1)).isoformat()

    yf_ticker = TICKER_ALIASES.get(ticker, ticker)
    print(f"Ticker  : {ticker}" + (f" (yfinance: {yf_ticker})" if yf_ticker != ticker else ""))
    print(f"Periodo : {start} -> {end}")

    tk = yf.Ticker(yf_ticker)
    df = tk.history(start=start, end=end_exclusive, auto_adjust=True)

    if df.empty:
        print(f"ERRORE: nessun dato yfinance per {ticker}")
        sys.exit(1)

    prices = [
        {"date": str(idx.date()), "close": round(float(row["Close"]), 2)}
        for idx, row in df.iterrows()
    ]

    DATA_DIR.mkdir(exist_ok=True)
    output = {"ticker": ticker, "start": start, "end": end, "prices": prices}
    with open(DATA_DIR / "prices.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Scaricati {len(prices)} giorni di prezzi -> data/prices.json")


if __name__ == "__main__":
    main()
