"""Pure normalization logic for the research dataset.

The module accepts the JSON payloads stored in GCS and returns a normalized,
storage-agnostic snapshot. It performs no network or BigQuery I/O, making this
interface the shared test surface for local runs and GitHub Actions.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


NEWS_TICKER_ALIASES = {"^GSPC": "SPY"}
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "guccounter",
    "guce_referrer",
    "guce_referrer_sig",
    "mc_cid",
    "mc_eid",
    "ocid",
    "ref",
    "referrer",
    "source",
}


@dataclass(frozen=True)
class NormalizedDataset:
    articles: list[dict[str, Any]]
    article_tickers: list[dict[str, Any]]
    prices: list[dict[str, Any]]
    metrics: dict[str, int]


def canonicalize_url(raw: str) -> str:
    """Return a stable URL used as the primary article identity."""
    if not raw or not raw.strip():
        return ""

    parts = urlsplit(raw.strip())
    host = parts.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]

    path = parts.path.rstrip("/") or "/"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query.append((key, value))

    return urlunsplit(
        (parts.scheme.casefold(), host, path, urlencode(sorted(query)), "")
    )


def normalize_dataset(
    ticker_payloads: dict[str, dict],
    price_payloads: dict[str, dict],
    as_of: date,
) -> NormalizedDataset:
    """Normalize one immutable snapshot of the raw GCS payloads.

    Articles are unique by canonical URL, with a headline/source/date fallback
    for legacy records without URLs. Relationships remain unique per
    ``(article_id, ticker)`` so one story can legitimately describe many stocks.
    """
    metrics = Counter(
        {
            "raw_article_occurrences": 0,
            "future_article_rows_filtered": 0,
            "missing_url_rows": 0,
            "exact_duplicate_rows_within_ticker": 0,
            "canonical_duplicate_rows_within_ticker": 0,
            "reused_canonical_urls": 0,
            "cross_ticker_duplicate_occurrences": 0,
            "candidate_story_duplicate_rows": 0,
            "article_conflicts": 0,
            "missing_ticker_associations": 0,
            "relationship_value_conflicts": 0,
            "raw_price_rows": 0,
            "future_price_rows_filtered": 0,
            "duplicate_price_rows": 0,
        }
    )

    articles_by_id: dict[str, dict[str, Any]] = {}
    overall_scores: dict[str, list[float]] = defaultdict(list)
    relationship_samples: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    article_ids_by_ticker: dict[str, set[str]] = defaultdict(set)
    tracked_tickers = set(ticker_payloads)

    # URL is the primary identity. Only URLs proven by this snapshot to host
    # multiple date/title variants are split into separate stories.
    variants_by_url: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for payload in ticker_payloads.values():
        for raw_article in payload.get("articles", []):
            article_date = _parse_iso_date(raw_article.get("date"), "article")
            if article_date > as_of:
                continue
            canonical_url = canonicalize_url(raw_article.get("url") or "")
            if canonical_url:
                variants_by_url[canonical_url].add(
                    (
                        article_date.isoformat(),
                        _normalize_text(raw_article.get("headline", "")),
                    )
                )
    reused_urls = {
        url for url, variants in variants_by_url.items() if len(variants) > 1
    }
    metrics["reused_canonical_urls"] = len(reused_urls)

    for ticker in sorted(ticker_payloads):
        payload = ticker_payloads[ticker]
        seen_exact: set[tuple[str, str, str]] = set()
        seen_canonical: set[tuple[str, str, str]] = set()

        for raw_article in payload.get("articles", []):
            metrics["raw_article_occurrences"] += 1
            article_date = _parse_iso_date(raw_article.get("date"), "article")
            if article_date > as_of:
                metrics["future_article_rows_filtered"] += 1
                continue

            original_url = (raw_article.get("url") or "").strip()
            canonical_url = canonicalize_url(original_url)
            if not original_url:
                metrics["missing_url_rows"] += 1

            story_identity = (
                article_date.isoformat(),
                _normalize_text(raw_article.get("headline", "")),
            )
            identity_suffix = story_identity if canonical_url in reused_urls else ()
            exact_key = (original_url, *identity_suffix)
            canonical_key = (canonical_url, *identity_suffix)

            duplicate_within_ticker = False
            if original_url and exact_key in seen_exact:
                metrics["exact_duplicate_rows_within_ticker"] += 1
                duplicate_within_ticker = True
            elif original_url:
                seen_exact.add(exact_key)

            if canonical_url and canonical_key in seen_canonical:
                if not duplicate_within_ticker:
                    metrics["canonical_duplicate_rows_within_ticker"] += 1
                duplicate_within_ticker = True
            elif canonical_url:
                seen_canonical.add(canonical_key)

            article_id = _article_id(
                raw_article, canonical_url, canonical_url in reused_urls
            )
            candidate = _article_record(raw_article, article_id, canonical_url)

            existing = articles_by_id.get(article_id)
            if existing is None:
                articles_by_id[article_id] = candidate
            else:
                if _article_fingerprint(existing) != _article_fingerprint(candidate):
                    metrics["article_conflicts"] += 1
                articles_by_id[article_id] = _merge_article(existing, candidate)

            if duplicate_within_ticker:
                continue

            article_ids_by_ticker[ticker].add(article_id)
            overall_scores[article_id].append(candidate["overall_sentiment"])

            ticker_sentiment = _ticker_sentiment(raw_article, ticker)
            if ticker_sentiment is None:
                metrics["missing_ticker_associations"] += 1

            for related_ticker, raw_sentiment in _tracked_ticker_sentiments(
                raw_article, tracked_tickers
            ).items():
                relationship_samples[(article_id, related_ticker)].append(
                    _relationship_record(article_id, related_ticker, raw_sentiment)
                )

    unique_ticker_occurrences = sum(len(ids) for ids in article_ids_by_ticker.values())
    metrics["cross_ticker_duplicate_occurrences"] = (
        unique_ticker_occurrences - len(articles_by_id)
    )

    relationships = []
    for samples in relationship_samples.values():
        distinct = {
            (row["sentiment"], row["relevance"], row["weighted_sentiment"])
            for row in samples
        }
        if len(distinct) > 1:
            metrics["relationship_value_conflicts"] += 1
        relationships.append(_mean_relationship(samples))

    association_counts = Counter(row["article_id"] for row in relationships)
    article_tickers = []
    for row in relationships:
        associated = association_counts[row["article_id"]]
        article_tickers.append(
            {
                **row,
                "associated_tickers": associated,
                "article_weight": round(1 / associated, 8),
            }
        )

    story_groups = Counter(
        (row["date"], _normalize_text(row["headline"]))
        for row in articles_by_id.values()
        if row["headline"]
    )
    metrics["candidate_story_duplicate_rows"] = sum(
        count - 1 for count in story_groups.values() if count > 1
    )

    for article_id, scores in overall_scores.items():
        mean_score = round(sum(scores) / len(scores), 8)
        articles_by_id[article_id]["overall_sentiment"] = mean_score
        articles_by_id[article_id]["overall_label"] = _sentiment_label(mean_score)

    prices = _normalize_prices(price_payloads, as_of, metrics)
    articles = sorted(
        articles_by_id.values(), key=lambda row: (row["date"], row["article_id"])
    )
    article_tickers.sort(key=lambda row: (row["ticker"], row["article_id"]))

    metrics.update(
        {
            "unique_articles": len(articles),
            "article_ticker_relationships": len(article_tickers),
            "price_rows": len(prices),
        }
    )
    return NormalizedDataset(articles, article_tickers, prices, dict(metrics))


def _article_id(raw_article: dict, canonical_url: str, reused_url: bool) -> str:
    if canonical_url:
        identity = f"url:{canonical_url}"
        if reused_url:
            identity += "|{}|{}".format(
                raw_article.get("date", ""),
                _normalize_text(raw_article.get("headline", "")),
            )
    else:
        identity = "fallback:{}|{}|{}".format(
            raw_article.get("date", ""),
            _normalize_text(raw_article.get("source", "")),
            _normalize_text(raw_article.get("headline", "")),
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _article_record(raw: dict, article_id: str, canonical_url: str) -> dict:
    return {
        "article_id": article_id,
        "date": raw.get("date", ""),
        "headline": (raw.get("headline") or "").strip(),
        "summary": (raw.get("summary") or "").strip(),
        "source": (raw.get("source") or "").strip(),
        "canonical_url": canonical_url,
        "original_url": (raw.get("url") or "").strip(),
        "overall_sentiment": _parse_float(
            raw.get("overall_sentiment"), "overall_sentiment"
        ),
        "overall_label": raw.get("overall_label") or "neutral",
        "topics": sorted(set(raw.get("topics") or [])),
    }


def _article_fingerprint(row: dict) -> tuple:
    return (
        row["date"],
        _normalize_text(row["headline"]),
        row["source"],
        row["canonical_url"],
    )


def _merge_article(left: dict, right: dict) -> dict:
    """Merge duplicate source records without making ingestion order meaningful."""
    preferred = min(
        (left, right),
        key=lambda row: (_article_fingerprint(row), row["original_url"]),
    )
    return {
        **preferred,
        "summary": max(
            (left["summary"], right["summary"]), key=lambda text: (len(text), text)
        ),
        "topics": sorted(set(left["topics"]) | set(right["topics"])),
    }


def _ticker_sentiment(raw_article: dict, file_ticker: str) -> dict | None:
    expected = NEWS_TICKER_ALIASES.get(file_ticker, file_ticker)
    matches = [
        item
        for item in raw_article.get("all_tickers", [])
        if item.get("ticker") == expected
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: _parse_float(item.get("relevance"), "relevance"),
    )


def _tracked_ticker_sentiments(
    raw_article: dict, tracked_tickers: set[str]
) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for item in raw_article.get("all_tickers", []):
        raw_ticker = item.get("ticker")
        ticker = "^GSPC" if raw_ticker == "SPY" and "^GSPC" in tracked_tickers else raw_ticker
        if ticker not in tracked_tickers:
            continue
        current = result.get(ticker)
        if current is None or _parse_float(
            item.get("relevance"), "relevance"
        ) > _parse_float(current.get("relevance"), "relevance"):
            result[ticker] = item
    return result


def _relationship_record(article_id: str, ticker: str, raw: dict) -> dict:
    sentiment = _parse_float(raw.get("sentiment"), "sentiment")
    relevance = _parse_float(raw.get("relevance"), "relevance")
    return {
        "article_id": article_id,
        "ticker": ticker,
        "sentiment": sentiment,
        "relevance": relevance,
        "label": raw.get("label") or _sentiment_label(sentiment),
        "weighted_sentiment": round(sentiment * relevance, 8),
    }


def _mean_relationship(samples: list[dict]) -> dict:
    sentiment = round(sum(row["sentiment"] for row in samples) / len(samples), 8)
    relevance = round(sum(row["relevance"] for row in samples) / len(samples), 8)
    weighted = round(
        sum(row["weighted_sentiment"] for row in samples) / len(samples), 8
    )
    return {
        "article_id": samples[0]["article_id"],
        "ticker": samples[0]["ticker"],
        "sentiment": sentiment,
        "relevance": relevance,
        "label": _sentiment_label(sentiment),
        "weighted_sentiment": weighted,
    }


def _normalize_prices(
    price_payloads: dict[str, dict], as_of: date, metrics: Counter
) -> list[dict]:
    rows_by_key: dict[tuple[str, str], dict] = {}
    for ticker in sorted(price_payloads):
        for raw in price_payloads[ticker].get("prices", []):
            metrics["raw_price_rows"] += 1
            price_date = _parse_iso_date(raw.get("date"), "price")
            if price_date > as_of:
                metrics["future_price_rows_filtered"] += 1
                continue
            key = (ticker, price_date.isoformat())
            if key in rows_by_key:
                metrics["duplicate_price_rows"] += 1
            rows_by_key[key] = {
                "ticker": ticker,
                "date": price_date.isoformat(),
                "close": _parse_float(raw.get("close"), "close"),
                "daily_return": (
                    None
                    if raw.get("return") is None
                    else _parse_float(raw.get("return"), "return")
                ),
            }
    return [rows_by_key[key] for key in sorted(rows_by_key)]


def _parse_iso_date(raw: Any, kind: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {kind} date: {raw!r}") from exc


def _normalize_text(raw: str) -> str:
    return " ".join((raw or "").casefold().split())


def _parse_float(raw: Any, field: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric {field}: {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"Non-finite numeric {field}: {raw!r}")
    return value


def _sentiment_label(score: float) -> str:
    if score > 0.1:
        return "positive"
    if score < -0.1:
        return "negative"
    return "neutral"
