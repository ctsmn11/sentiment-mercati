# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Sentiment Mercati** — a research tool that correlates financial news sentiment with historical market prices. The central research question: *do negative news precede market crashes, or do crashes generate negative news?*

See [Piano.md](Piano.md) for the full specification (in Italian).

## Research Finding

Granger causality test (100 days, S&P 500) shows:
- **Market → News**: significant at all lags 1–5 (p < 0.025)
- **News → Market**: not significant at any lag (p > 0.18)

Conclusion: price movements precede news sentiment, not the reverse. Journalists react to the market, not the other way around.

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

## Commands

### First-time setup

```bat
cd backend
pip install -r requirements.txt
```

```bash
cd frontend
npm install
```

### Pipeline (run before starting the server)

```bash
cd backend

# 1. Scarica news + sentiment da Alpha Vantage (usa ~13 chiamate API per 365 giorni)
python classify_news.py --days 365

# 2. Scarica prezzi storici da Yahoo Finance
python fetch_prices.py

# 3. Calcola correlazione Pearson + Granger
python compute_correlation.py

# (opzionale) Valida la qualita' del dato
python validate_news.py
```

### Dev server (two terminals)

```bash
# Terminal 1 — backend
cd backend
uvicorn main:app --reload

# Terminal 2 — frontend
cd frontend
npm run dev
```

Open http://localhost:5173.

### Environment

Create `backend/.env` (see `.env.example`):
```
ALPHAVANTAGE_KEY=...   # gratis su alphavantage.co
TICKER=^GSPC
```

## Architecture

```
[Alpha Vantage NEWS_SENTIMENT API]
        |
        v
[classify_news.py]
  - fetches news in 30-day windows (rate limit: 1 req/s, 25/day)
  - uses ticker_sentiment_score as relevance-weighted sentiment
  - writes data/news_raw.json + data/news_classified.json
        |
        v
[fetch_prices.py]
  - downloads OHLCV from Yahoo Finance for the same date range
  - writes data/prices.json
        |
        v
[compute_correlation.py]
  - aggregates daily sentiment = mean(sentiment * relevance)
  - computes daily returns = (close_t - close_{t-1}) / close_{t-1}
  - Pearson correlation for lag -5..+5
  - Granger causality test (both directions, maxlag=5)
  - writes data/correlations.json
        |
        v
[main.py + FastAPI /api/data]
  - reads news_classified.json + fetches prices on demand
  - serves { news, prices } to the frontend
        |
        v
[Frontend: MarketChart + NewsPanel]
```

## Key Files

| File | Responsibility |
|------|---------------|
| `backend/classify_news.py` | Fetch news + sentiment from Alpha Vantage, write `news_classified.json` |
| `backend/fetch_prices.py` | Download price history from Yahoo Finance, write `prices.json` |
| `backend/compute_correlation.py` | Pearson lag analysis + Granger causality test, write `correlations.json` |
| `backend/validate_news.py` | One-off qualitative data validation report |
| `backend/main.py` | FastAPI server — reads `news_classified.json`, serves `/api/data` |
| `frontend/src/hooks/useSentimentData.js` | Fetches `/api/data`, exposes `{ news, prices, loading, error }` |
| `frontend/src/components/MarketChart.jsx` | Dual-axis Recharts chart (price line + sentiment bars) |
| `frontend/src/components/NewsPanel.jsx` | Sorted list of headlines with sentiment badge and score |

## Sentiment Score

Alpha Vantage provides `ticker_sentiment_score` in range [−1, +1].

Effective daily score: `mean(sentiment * relevance)` across all articles for that day.

Thresholds: `score > 0.1` → positive, `score < -0.1` → negative, else neutral.

## Data Quality Notes

- Coverage is sparse before Oct 2025 (1–4 articles/month); denser from Oct 2025 onward
- Alpha Vantage occasionally assigns high relevance to tangential articles (e.g., personal finance pieces that mention SPY indirectly)
- Granger results are robust to the ~2–3 low-quality articles identified in validation

## Important Notes

- **CORS**: backend allows only `http://localhost:5173`; update if deploying
- **Rate limit**: Alpha Vantage free tier = 25 req/day, 1 req/s. The pipeline uses ~13 calls for 365 days
- **Data files**: `backend/data/` is git-ignored — regenerate with the pipeline commands above
