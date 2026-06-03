"""
compute_correlation.py — correlazione Pearson + test di Granger tra sentiment e rendimenti

Pearson con lag -5..+5:
  lag > 0 -> il sentiment anticipa il mercato
  lag < 0 -> il mercato anticipa il sentiment
  lag = 0 -> correlazione contemporanea

Granger (maxlag=5), due direzioni:
  sentiment -> returns: le notizie causano movimenti di mercato?
  returns -> sentiment: i movimenti causano le notizie?

Output: data/correlations.json
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import stats
from statsmodels.tsa.stattools import grangercausalitytests

DATA_DIR = Path(__file__).parent / "data"


def load_data():
    classified_path = DATA_DIR / "news_classified.json"
    prices_path = DATA_DIR / "prices.json"

    if not classified_path.exists():
        print("ERRORE: data/news_classified.json non trovato. Esegui prima la classificazione.")
        sys.exit(1)
    if not prices_path.exists():
        print("ERRORE: data/prices.json non trovato. Esegui prima fetch_prices.py.")
        sys.exit(1)

    with open(classified_path, encoding="utf-8") as f:
        classified = json.load(f)
    with open(prices_path, encoding="utf-8") as f:
        prices_data = json.load(f)

    return classified, prices_data


def aggregate_daily_sentiment(
    articles: list[dict],
    trading_dates: set[str] | None = None,
) -> dict[str, float]:
    """
    Aggrega il sentiment per giorno.
    Se trading_dates e' fornito, gli articoli su weekend/festivi vengono
    shiftati al prossimo giorno di borsa disponibile.
    """
    if trading_dates:
        sorted_td = sorted(trading_dates)

    daily = defaultdict(list)
    for art in articles:
        d = art["date"]
        if trading_dates and d not in trading_dates:
            # Shift al prossimo giorno di mercato
            future = [t for t in sorted_td if t > d]
            if not future:
                continue
            d = future[0]
        effective = float(art["sentiment"]) * float(art["relevance"])
        daily[d].append(effective)
    return {d: round(sum(v) / len(v), 4) for d, v in daily.items()}


def compute_returns(prices: list[dict]) -> dict[str, float]:
    sorted_prices = sorted(prices, key=lambda x: x["date"])
    returns = {}
    for i in range(1, len(sorted_prices)):
        d = sorted_prices[i]["date"]
        prev_close = sorted_prices[i - 1]["close"]
        curr_close = sorted_prices[i]["close"]
        returns[d] = round((curr_close - prev_close) / prev_close, 6)
    return returns


def pearson_with_lag(sentiment: list, returns: list, lag: int):
    if lag > 0:
        s, r = sentiment[:-lag], returns[lag:]
    elif lag < 0:
        s, r = sentiment[-lag:], returns[:lag]
    else:
        s, r = sentiment, returns

    n = len(s)
    if n < 5:
        return None

    corr, pvalue = stats.pearsonr(s, r)
    return {"lag": lag, "correlation": round(float(corr), 4), "pvalue": round(float(pvalue), 4),
            "significant": bool(pvalue < 0.05), "n": n}


def granger_test(sentiment: list, returns: list, maxlag: int = 5) -> dict:
    """
    Testa la causalita' di Granger in entrambe le direzioni.
    Restituisce dict con chiavi 'sentiment_causes_returns' e 'returns_cause_sentiment',
    ciascuna lista di {lag, f_stat, pvalue, significant}.
    """
    arr_s = np.array(sentiment)
    arr_r = np.array(returns)

    def _run(y, x, label):
        data = np.column_stack([y, x])
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = grangercausalitytests(data, maxlag=maxlag, verbose=False)
        except Exception as e:
            print(f"  [warn] Granger {label}: {e}")
            return []
        out = []
        for lag, res in raw.items():
            f_stat, pvalue, _, _ = res[0]["ssr_ftest"]
            out.append({
                "lag": int(lag),
                "f_stat": round(float(f_stat), 4),
                "pvalue": round(float(pvalue), 4),
                "significant": bool(pvalue < 0.05),
            })
        return out

    return {
        "sentiment_causes_returns": _run(arr_r, arr_s, "sentiment->returns"),
        "returns_cause_sentiment":  _run(arr_s, arr_r, "returns->sentiment"),
    }


def main():
    classified, prices_data = load_data()
    ticker = classified.get("ticker", "?")

    trading_dates = {p["date"] for p in prices_data["prices"]}
    returns_by_day = compute_returns(prices_data["prices"])
    all_trading = sorted(returns_by_day)  # giorni con rendimento calcolabile

    # Shift articoli weekend/festivi al prossimo giorno di borsa
    sentiment_by_day = aggregate_daily_sentiment(classified["articles"], trading_dates)

    # --- Pearson: solo giorni con sia sentiment che rendimento ---
    common_dates = sorted(set(sentiment_by_day) & set(returns_by_day))
    if len(common_dates) < 5:
        print(f"ERRORE: solo {len(common_dates)} giorni in comune — dati insufficienti per la correlazione")
        sys.exit(1)

    sentiment_series = [sentiment_by_day[d] for d in common_dates]
    returns_series   = [returns_by_day[d]   for d in common_dates]

    results = []
    for lag in range(-5, 6):
        r = pearson_with_lag(sentiment_series, returns_series, lag)
        if r:
            results.append(r)

    # --- Granger: serie completa su tutti i giorni di borsa, sentiment forward-filled ---
    # Dove mancano notizie, il sentiment dell'ultimo giorno disponibile persiste.
    full_sentiment = []
    last_s = 0.0
    for d in all_trading:
        if d in sentiment_by_day:
            last_s = sentiment_by_day[d]
        full_sentiment.append(last_s)
    full_returns = [returns_by_day[d] for d in all_trading]

    granger = granger_test(full_sentiment, full_returns)
    print(f"Pearson su {len(common_dates)} giorni comuni, Granger su {len(all_trading)} giorni di borsa (forward-fill)")

    output = {
        "ticker": ticker,
        "n_days": len(common_dates),
        "date_range": {"start": common_dates[0], "end": common_dates[-1]},
        "correlations": results,
        "granger": granger,
    }

    with open(DATA_DIR / "correlations.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # --- Pearson ---
    print(f"\nCorrelazione sentiment <-> rendimenti -- {ticker}")
    print(f"Giorni analizzati: {len(common_dates)} ({common_dates[0]} -> {common_dates[-1]})")
    print(f"\n{'Lag':>5}  {'Pearson r':>10}  {'p-value':>8}  {'Sig.':>5}")
    print("-" * 38)
    for r in results:
        sig = "*" if r["significant"] else ""
        print(f"{r['lag']:>5}  {r['correlation']:>10.4f}  {r['pvalue']:>8.4f}  {sig:>5}")

    best = max(results, key=lambda x: abs(x["correlation"]))
    print(f"\nCorrelazione massima: lag={best['lag']}, r={best['correlation']}, p={best['pvalue']}")

    # --- Granger ---
    print(f"\n{'='*50}")
    print("TEST DI GRANGER (F-test, maxlag=5)")
    print(f"{'='*50}")
    for direction, label in [
        ("sentiment_causes_returns", "Sentiment  ->  Rendimenti  (news causano mercato?)"),
        ("returns_cause_sentiment",  "Rendimenti ->  Sentiment   (mercato causa news?)"),
    ]:
        print(f"\n  {label}")
        print(f"  {'Lag':>4}  {'F-stat':>8}  {'p-value':>8}  {'Sig.':>5}")
        print("  " + "-" * 32)
        for row in granger[direction]:
            sig = "*" if row["significant"] else ""
            print(f"  {row['lag']:>4}  {row['f_stat']:>8.4f}  {row['pvalue']:>8.4f}  {sig:>5}")

    s_sig = any(r["significant"] for r in granger["sentiment_causes_returns"])
    r_sig = any(r["significant"] for r in granger["returns_cause_sentiment"])
    print(f"\nConclusione Granger:")
    if s_sig and r_sig:
        print("  Causalita' bidirezionale: news e mercato si influenzano a vicenda")
    elif s_sig:
        print("  Le news Granger-causano i rendimenti (news -> mercato)")
    elif r_sig:
        print("  I rendimenti Granger-causano il sentiment (mercato -> news)")
    else:
        print("  Nessuna causalita' di Granger significativa rilevata")

    print(f"\nSalvato -> data/correlations.json")


if __name__ == "__main__":
    main()
