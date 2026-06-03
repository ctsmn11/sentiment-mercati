"""
validate_news.py — revisione qualitativa del dato news_classified.json

Produce un report leggibile per validare manualmente:
  - articoli piu' positivi / piu' negativi
  - articoli ad alta rilevanza
  - possibili errori di sentiment (score estremo vs. tono del titolo)
  - distribuzione per data e label

Esegui: python validate_news.py
"""

import json
from pathlib import Path
from collections import Counter

DATA_DIR = Path(__file__).parent / "data"


def load():
    with open(DATA_DIR / "news_classified.json", encoding="utf-8") as f:
        return json.load(f)


def sep(title="", width=62):
    if title:
        pad = width - len(title) - 2
        print(f"\n{'='*((pad)//2)} {title} {'='*(pad - (pad)//2)}")
    else:
        print("=" * width)


def show_articles(articles, title, n=15):
    sep(title)
    for a in articles[:n]:
        eff = round(a["sentiment"] * a["relevance"], 3)
        print(f"  [{a['date']}] sent={a['sentiment']:+.3f} rel={a['relevance']:.2f} eff={eff:+.3f}")
        print(f"    {a['headline'][:100]}")


def main():
    data = load()
    articles = data["articles"]
    ticker = data["ticker"]

    print(f"\nValidazione: {ticker}  |  {data['start']} -> {data['end']}")
    print(f"Totale articoli: {len(articles)}")

    # --- Distribuzione label ---
    sep("DISTRIBUZIONE LABEL")
    labels = Counter(a["label"] for a in articles)
    for label, count in sorted(labels.items()):
        pct = count / len(articles) * 100
        bar = "#" * (count // 2)
        print(f"  {label:10s} {count:4d} ({pct:5.1f}%)  {bar}")

    # --- Distribuzione per mese ---
    sep("ARTICOLI PER MESE")
    by_month = Counter(a["date"][:7] for a in articles)
    for month in sorted(by_month):
        count = by_month[month]
        bar = "#" * count
        print(f"  {month}  {count:3d}  {bar}")

    # --- Top positivi ---
    top_pos = sorted(articles, key=lambda a: a["sentiment"] * a["relevance"], reverse=True)
    show_articles(top_pos, "TOP 15 PIU' POSITIVI (sentiment * relevance)")

    # --- Top negativi ---
    top_neg = sorted(articles, key=lambda a: a["sentiment"] * a["relevance"])
    show_articles(top_neg, "TOP 15 PIU' NEGATIVI (sentiment * relevance)")

    # --- Alta rilevanza ---
    high_rel = sorted([a for a in articles if a["relevance"] >= 0.9],
                      key=lambda a: abs(a["sentiment"]), reverse=True)
    show_articles(high_rel, "ALTA RILEVANZA (>= 0.9)", n=20)

    # --- Potenziali outlier: sentiment estremo (|score| > 0.5) ---
    sep("SENTIMENT ESTREMO (|score| > 0.5)")
    extremes = [a for a in articles if abs(a["sentiment"]) > 0.5]
    extremes.sort(key=lambda a: a["sentiment"])
    for a in extremes:
        print(f"  [{a['date']}] {a['sentiment']:+.3f}  {a['headline'][:100]}")

    # --- Possibile rumore: rilevanza bassa ma sentiment alto ---
    sep("RUMORE POTENZIALE (rel < 0.4 e |sent| > 0.2)")
    noise = [a for a in articles if a["relevance"] < 0.4 and abs(a["sentiment"]) > 0.2]
    noise.sort(key=lambda a: a["relevance"])
    for a in noise:
        print(f"  [{a['date']}] rel={a['relevance']:.2f} sent={a['sentiment']:+.3f}  {a['headline'][:100]}")

    # --- Sentiment score = 0 esatti (sospetti) ---
    zeros = [a for a in articles if a["sentiment"] == 0.0]
    if zeros:
        sep(f"SENTIMENT = 0.0 ESATTI ({len(zeros)} articoli)")
        for a in zeros:
            print(f"  [{a['date']}] rel={a['relevance']:.2f}  {a['headline'][:90]}")

    sep()
    print("Report completato. Revisione qualitativa richiesta.")


if __name__ == "__main__":
    main()
