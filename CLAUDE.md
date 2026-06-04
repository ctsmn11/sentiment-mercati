# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Sentiment Mercati** — a research tool that correlates financial news sentiment with historical market prices. The central research question: *do negative news precede market crashes, or do crashes generate negative news?*

See [Piano.md](Piano.md) for the full specification (in Italian).

## Research Finding

Granger causality test (100 days, S&P 500) shows:
- **Market → News**: significant at all lags 1–5 (p < 0.025)
- **News → Market**: not significant at any lag (p > 0.18)

Conclusion: price movements precede news sentiment, not the reverse.

## Development Phases

1. **Phase 1** ✅ — Hard-coded synthetic news + FinBERT sentiment + Yahoo Finance prices → dual-axis chart
2. **Phase 2** ✅ — Real Yahoo Finance prices via `yfinance`
3. **Phase 3** ✅ — Real news + sentiment from Alpha Vantage NEWS_SENTIMENT API
4. **Phase 4** ✅ — Pearson correlation (lag −5…+5) + Granger causality test
5. **Phase 5** — Ticker/period selectors, interactive tooltips, news side panel, color indicators

## Tech Stack

- **Frontend**: React + Recharts + Tailwind CSS, served by Vite (port 5173)
- **Backend**: Python FastAPI (port 8000), proxied by Vite `/api → localhost:8000`
- **News + Sentiment**: [Alpha Vantage NEWS_SENTIMENT API](https://www.alphavantage.co/documentation/#news-sentiment) — free tier, 25 req/day, sentiment pre-computed
- **Prices**: `yfinance`
- **Statistics**: `scipy` (Pearson), `statsmodels` (Granger)

## Two Pipeline Variants

There are two separate pipeline directories with different purposes:

### `pipeline/` — Production (GitHub Actions)

Used by the daily CI job (`collect.yml`). Handles **50 S&P 500 tickers** + `^GSPC` index defined in `pipeline/tickers.json`. Writes data **committed to the repo** at `pipeline/data/`.

```bash
cd pipeline
pip install -r requirements.txt

# Collect news + sentiment (processes tickers by staleness, respects 25 req/day budget)
python classify_news.py

# Download prices for all tickers
python fetch_prices.py

# Quarterly: sync tickers.json with current SPDR top-50 holdings
python update_tickers.py
```

Data layout:
- `pipeline/data/news.json` — all articles across all tickers, deduped by URL, with `last_updated` checkpoint per ticker
- `pipeline/data/{TICKER}/prices.json` — per-ticker OHLCV + daily returns

### `backend/` — Local dev (single ticker)

Used for local development. Handles one ticker at a time from `backend/.env`. Writes to `backend/data/` (git-ignored).

```bash
cd backend
pip install -r requirements.txt

python classify_news.py --days 365   # ~13 API calls
python fetch_prices.py
python compute_correlation.py
python validate_news.py              # optional quality report
```

### Dev server

```bash
# Terminal 1 — backend
cd backend
uvicorn main:app --reload

# Terminal 2 — frontend
cd frontend
npm run dev
```

After running the local pipeline, copy the data files to the frontend's public dir:

```bash
cd frontend
npm run sync-data   # cp ../backend/data/*.json public/data/
```

Open http://localhost:5173.

### Environment

Create `backend/.env` (see `.env.example`):
```
ALPHAVANTAGE_KEY=...
TICKER=^GSPC
```

## Architecture

```
[GitHub Actions: collect.yml — daily 18:00 UTC]
        |
        v
[pipeline/classify_news.py]
  - iterates tickers by staleness (most stale first)
  - 30-day windows, 1 API call/window, max 25/day
  - dedup by URL across all tickers
  - checkpoint advances per window (atomic writes)
  - writes pipeline/data/news.json
        |
        v
[pipeline/fetch_prices.py]
  - incremental: appends only missing days
  - atomic write (tmp + rename) to prevent corrupt JSON on CI kill
  - writes pipeline/data/{TICKER}/prices.json (includes daily returns)
        |
[Committed to repo]
        |
        v
[backend/main.py + FastAPI /api/data]
  - reads backend/data/news_classified.json
  - fetches prices live from yfinance
  - in-process cache (_cache) — cleared on server restart
  - serves { news, prices } to the frontend
        |
        v
[frontend: useDashboardData.js]
  - fetches /data/news_classified.json, /data/prices.json, /data/correlations.json
    from frontend/public/data/ (static files, not the API)
  - exposes { data, loading, error }
        |
        v
[MarketChart + CorrelationChart + GrangerPanel + NewsPanel]
```

Note: `useSentimentData.js` is a legacy hook that calls `/api/data`; it is not used by `App.jsx`.

## Key Files

| File | Responsibility |
|------|---------------|
| `pipeline/classify_news.py` | Multi-ticker news collection, checkpoint-based, URL-deduped |
| `pipeline/fetch_prices.py` | Incremental per-ticker price download |
| `pipeline/update_tickers.py` | Quarterly sync of `tickers.json` with SPDR top-50 |
| `pipeline/utils.py` | Shared constants: `DATASET_START` (2026-01-01), file paths, `all_tickers()` |
| `pipeline/tickers.json` | 50 S&P 500 constituents + `^GSPC` market index |
| `backend/classify_news.py` | Single-ticker news fetch for local dev |
| `backend/compute_correlation.py` | Pearson lag analysis + Granger causality test → `correlations.json` |
| `backend/main.py` | FastAPI server with in-process cache, `TICKER_ALIASES` for `^VWCE` |
| `frontend/src/hooks/useDashboardData.js` | Loads static JSON from `public/data/`, exposes `{ data, loading, error }` |
| `frontend/src/components/MarketChart.jsx` | Dual-axis chart: price line + daily sentiment bars + tooltip with headlines |
| `frontend/src/components/CorrelationChart.jsx` | Pearson r bar chart for lag −5…+5, highlights significant bars |
| `frontend/src/components/GrangerPanel.jsx` | F-stat/p-value table for both Granger directions + conclusion text |
| `frontend/src/components/NewsPanel.jsx` | Sorted list of headlines with sentiment badge and score |

## Sentiment Score

Alpha Vantage provides `ticker_sentiment_score` in [−1, +1].

Effective daily score: `mean(sentiment * relevance)` across all articles for that day.

Thresholds: `score > 0.1` → positive, `score < -0.1` → negative, else neutral.

## Data Quality Notes

- `DATASET_START = 2026-01-01` in `pipeline/utils.py` — data before that date is not collected
- Coverage is sparse before Oct 2025 in the `backend/` local data (1–4 articles/month)
- Alpha Vantage `^GSPC` news requires using `SPY` as proxy (mapped in `NEWS_TICKER_ALIASES`)
- `^VWCE` requires Yahoo Finance alias `VWCE.AS` (mapped in `TICKER_ALIASES` in `main.py` and `fetch_prices.py`)
- When Alpha Vantage returns ≥1000 articles in a window, the checkpoint advances only to the last article date (not the window end), so the next run resumes without gaps

## Important Notes

- **CORS**: backend allows only `http://localhost:5173`; update if deploying
- **Rate limit**: Alpha Vantage free tier = 25 req/day. `pipeline/classify_news.py` tracks budget across tickers; partial progress is saved after every window
- **Atomic writes**: both pipeline scripts write to a `.json.tmp` then rename — prevents corrupt JSON if the CI job is killed mid-write
- **Data files**: `backend/data/` is git-ignored; `pipeline/data/` is committed to the repo by the CI job
