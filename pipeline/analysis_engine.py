"""Pure statistical analysis for the normalized market/news dataset."""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any
import warnings

import numpy as np
from scipy import stats
from statsmodels.tools.sm_exceptions import InfeasibleTestError
from statsmodels.tsa.stattools import adfuller, grangercausalitytests


@dataclass(frozen=True)
class AnalysisResult:
    daily_series: list[dict[str, Any]]
    pearson: list[dict[str, Any]]
    granger: list[dict[str, Any]]
    downside_events: list[dict[str, Any]]
    ticker_summary: list[dict[str, Any]]


def analyze_market_data(
    articles: list[dict[str, Any]],
    article_tickers: list[dict[str, Any]],
    prices: list[dict[str, Any]],
    *,
    max_lag: int = 5,
    alpha: float = 0.05,
) -> AnalysisResult:
    """Analyze every ticker through one stable, side-effect-free interface."""
    if max_lag < 1:
        raise ValueError("max_lag must be at least 1")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")

    article_dates = {
        str(row["article_id"]): _iso_date(row["date"]) for row in articles
    }
    prices_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in prices:
        prices_by_ticker[str(row["ticker"])].append(row)
    relationships_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in article_tickers:
        relationships_by_ticker[str(row["ticker"])].append(row)

    daily_series: list[dict[str, Any]] = []
    for ticker in sorted(prices_by_ticker):
        ticker_prices = sorted(
            prices_by_ticker[ticker], key=lambda row: _iso_date(row["date"])
        )
        trading_dates = [_iso_date(row["date"]) for row in ticker_prices]
        sentiment_by_session: dict[str, list[float]] = defaultdict(list)
        for relation in relationships_by_ticker.get(ticker, []):
            article_date = article_dates.get(str(relation["article_id"]))
            if article_date is None:
                continue
            index = bisect_left(trading_dates, article_date)
            if index == len(trading_dates):
                continue
            sentiment_by_session[trading_dates[index]].append(
                float(relation["weighted_sentiment"])
            )

        previous_close: float | None = None
        for row, trading_date in zip(ticker_prices, trading_dates):
            close = float(row["close"])
            daily_return = row.get("daily_return")
            if daily_return is None and previous_close is not None:
                daily_return = (close - previous_close) / previous_close
            previous_close = close
            if daily_return is None:
                continue
            samples = sentiment_by_session.get(trading_date, [])
            daily_series.append(
                {
                    "ticker": ticker,
                    "date": trading_date,
                    "close": close,
                    "daily_return": float(daily_return),
                    "sentiment": sum(samples) / len(samples) if samples else 0.0,
                    "article_count": len(samples),
                }
            )

    pearson: list[dict[str, Any]] = []
    daily_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily_series:
        daily_by_ticker[row["ticker"]].append(row)
    for ticker, rows in daily_by_ticker.items():
        sentiment = [row["sentiment"] for row in rows]
        returns = [row["daily_return"] for row in rows]
        for lag in range(-max_lag, max_lag + 1):
            paired_sentiment, paired_returns = _lagged_pair(sentiment, returns, lag)
            if (
                len(paired_sentiment) < 5
                or len(set(paired_sentiment)) < 2
                or len(set(paired_returns)) < 2
            ):
                continue
            correlation, pvalue = stats.pearsonr(paired_sentiment, paired_returns)
            pearson.append(
                {
                    "ticker": ticker,
                    "lag": lag,
                    "direction": (
                        "news_to_market"
                        if lag > 0
                        else "market_to_news" if lag < 0 else "contemporaneous"
                    ),
                    "correlation": float(correlation),
                    "pvalue": float(pvalue),
                    "n": len(paired_sentiment),
                    "significant": bool(pvalue < alpha),
                }
            )

    _add_grouped_fdr(pearson, alpha)
    _add_fdr(
        pearson,
        alpha,
        qvalue_field="global_qvalue",
        significant_field="significant_global_fdr",
    )

    diagnostics: dict[str, dict[str, Any]] = {}
    granger_status: dict[str, dict[str, str]] = {}
    for ticker, rows in daily_by_ticker.items():
        sentiment_adf = _adf_pvalue([row["sentiment"] for row in rows])
        returns_adf = _adf_pvalue([row["daily_return"] for row in rows])
        diagnostics[ticker] = {
            "adf_pvalue_sentiment": sentiment_adf,
            "adf_pvalue_returns": returns_adf,
        }
        if len(rows) < max(20, 3 * max_lag + 5):
            initial_status = "insufficient_data"
        elif sentiment_adf is None or returns_adf is None:
            initial_status = "diagnostic_unavailable"
        elif sentiment_adf >= alpha or returns_adf >= alpha:
            initial_status = "nonstationary"
        else:
            initial_status = "pending"
        granger_status[ticker] = {
            "news_to_market": initial_status,
            "market_to_news": initial_status,
        }

    granger: list[dict[str, Any]] = []
    for ticker, rows in daily_by_ticker.items():
        sentiment = [row["sentiment"] for row in rows]
        returns = [row["daily_return"] for row in rows]
        if granger_status[ticker]["news_to_market"] != "pending":
            continue
        for direction, target, predictor in (
            ("news_to_market", returns, sentiment),
            ("market_to_news", sentiment, returns),
        ):
            if len(set(target)) < 2 or len(set(predictor)) < 2:
                granger_status[ticker][direction] = "constant_series"
                continue
            data = list(zip(target, predictor))
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    raw = grangercausalitytests(data, maxlag=max_lag, verbose=False)
            except (ValueError, np.linalg.LinAlgError, InfeasibleTestError):
                granger_status[ticker][direction] = "infeasible"
                continue
            granger_status[ticker][direction] = "completed"
            for lag, result in raw.items():
                f_stat, pvalue, _, _ = result[0]["ssr_ftest"]
                granger.append(
                    {
                        "ticker": ticker,
                        "direction": direction,
                        "lag": int(lag),
                        "f_stat": float(f_stat),
                        "pvalue": float(pvalue),
                    }
                )
    _add_grouped_fdr(granger, alpha)
    _add_fdr(
        granger,
        alpha,
        qvalue_field="global_qvalue",
        significant_field="significant_global_fdr",
    )

    downside_events: list[dict[str, Any]] = []
    event_window = 5
    for ticker, rows in daily_by_ticker.items():
        if len(rows) < event_window * 2 + 10:
            continue
        threshold = min(
            float(np.quantile([row["daily_return"] for row in rows], 0.05)),
            -0.02,
        )
        candidates = [
            index
            for index, row in enumerate(rows)
            if row["daily_return"] <= threshold
            and index >= event_window
            and index + event_window <= len(rows)
        ]
        # Keep the worst event in overlapping windows so one sell-off is not
        # counted repeatedly as several independent downside events.
        selected: list[int] = []
        for index in sorted(candidates, key=lambda item: rows[item]["daily_return"]):
            if all(abs(index - other) >= event_window * 2 for other in selected):
                selected.append(index)
        for index in sorted(selected):
            pre = rows[index - event_window : index]
            post = rows[index : index + event_window]
            pre_sentiment = sum(row["sentiment"] for row in pre) / event_window
            post_sentiment = sum(row["sentiment"] for row in post) / event_window
            downside_events.append(
                {
                    "ticker": ticker,
                    "date": rows[index]["date"],
                    "daily_return": rows[index]["daily_return"],
                    "event_threshold": threshold,
                    "pre_sentiment": pre_sentiment,
                    "post_sentiment": post_sentiment,
                    "sentiment_change": post_sentiment - pre_sentiment,
                    "pre_articles": sum(row["article_count"] for row in pre),
                    "post_articles": sum(row["article_count"] for row in post),
                    "window_sessions": event_window,
                }
            )

    ticker_summary: list[dict[str, Any]] = []
    for ticker, rows in sorted(daily_by_ticker.items()):
        ticker_granger = [row for row in granger if row["ticker"] == ticker]
        news_significant = any(
            row["direction"] == "news_to_market" and row["significant_fdr"]
            for row in ticker_granger
        )
        market_significant = any(
            row["direction"] == "market_to_news" and row["significant_fdr"]
            for row in ticker_granger
        )
        statuses = granger_status[ticker]
        if set(statuses.values()) != {"completed"}:
            conclusion = (
                "insufficient_data"
                if "insufficient_data" in statuses.values()
                else "invalid_diagnostics"
            )
        elif news_significant and market_significant:
            conclusion = "bidirectional"
        elif news_significant:
            conclusion = "news_to_market"
        elif market_significant:
            conclusion = "market_to_news"
        else:
            conclusion = "none"
        global_news = any(
            row["direction"] == "news_to_market"
            and row["significant_global_fdr"]
            for row in ticker_granger
        )
        global_market = any(
            row["direction"] == "market_to_news"
            and row["significant_global_fdr"]
            for row in ticker_granger
        )
        if set(statuses.values()) != {"completed"}:
            global_conclusion = conclusion
        elif global_news and global_market:
            global_conclusion = "bidirectional"
        elif global_news:
            global_conclusion = "news_to_market"
        elif global_market:
            global_conclusion = "market_to_news"
        else:
            global_conclusion = "none"
        ticker_events = [row for row in downside_events if row["ticker"] == ticker]
        event_pre = [row["pre_sentiment"] for row in ticker_events]
        event_post = [row["post_sentiment"] for row in ticker_events]
        mean_event_pre = (
            sum(event_pre) / len(event_pre) if event_pre else None
        )
        mean_event_post = (
            sum(event_post) / len(event_post) if event_post else None
        )
        event_pvalue = (
            float(stats.ttest_rel(event_post, event_pre).pvalue)
            if len(ticker_events) >= 2
            else None
        )
        ticker_summary.append(
            {
                "ticker": ticker,
                "observations": len(rows),
                "news_sessions": sum(row["article_count"] > 0 for row in rows),
                "articles": sum(row["article_count"] for row in rows),
                "start_date": rows[0]["date"],
                "end_date": rows[-1]["date"],
                "granger_conclusion": conclusion,
                "granger_global_conclusion": global_conclusion,
                "granger_news_to_market_status": statuses["news_to_market"],
                "granger_market_to_news_status": statuses["market_to_news"],
                **diagnostics[ticker],
                "downside_events": len(ticker_events),
                "mean_downside_pre_sentiment": mean_event_pre,
                "mean_downside_post_sentiment": mean_event_post,
                "mean_downside_sentiment_change": (
                    mean_event_post - mean_event_pre
                    if mean_event_post is not None and mean_event_pre is not None
                    else None
                ),
                "downside_pvalue": event_pvalue,
            }
        )

    return AnalysisResult(daily_series, pearson, granger, downside_events, ticker_summary)


def _iso_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _lagged_pair(
    sentiment: list[float], returns: list[float], lag: int
) -> tuple[list[float], list[float]]:
    # lag > 0 evaluates sentiment[t] against return[t + lag].
    if lag > 0:
        return sentiment[:-lag], returns[lag:]
    if lag < 0:
        return sentiment[-lag:], returns[:lag]
    return sentiment, returns


def _add_grouped_fdr(rows: list[dict[str, Any]], alpha: float) -> None:
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_ticker[row["ticker"]].append(row)
    for ticker_rows in by_ticker.values():
        _add_fdr(ticker_rows, alpha)


def _add_fdr(
    rows: list[dict[str, Any]],
    alpha: float,
    *,
    qvalue_field: str = "qvalue",
    significant_field: str = "significant_fdr",
) -> None:
    """Add Benjamini-Hochberg q-values across one family of tests."""
    if not rows:
        return
    ranked = sorted(enumerate(rows), key=lambda item: item[1]["pvalue"])
    count = len(ranked)
    adjusted = [1.0] * count
    running_minimum = 1.0
    for rank_index in range(count - 1, -1, -1):
        original_index, row = ranked[rank_index]
        rank = rank_index + 1
        running_minimum = min(running_minimum, row["pvalue"] * count / rank)
        adjusted[original_index] = min(1.0, running_minimum)
    for row, qvalue in zip(rows, adjusted):
        row[qvalue_field] = qvalue
        row[significant_field] = bool(qvalue < alpha)


def _adf_pvalue(values: list[float]) -> float | None:
    if len(values) < 20 or len(set(values)) < 2:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return float(adfuller(values, autolag="AIC")[1])
    except (ValueError, np.linalg.LinAlgError):
        return None
