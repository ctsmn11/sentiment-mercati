import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from pipeline.dataset_builder import canonicalize_url, normalize_dataset
from pipeline.build_dataset import (
    BigQueryPublisher,
    build_version,
    dataset_fingerprint,
    validate_dataset_id,
    validate_normalized,
)


def article(
    url,
    *,
    ticker="AAPL",
    date_="2026-01-10",
    headline="Example headline",
    overall=-0.2,
):
    return {
        "date": date_,
        "headline": headline,
        "summary": "A useful summary",
        "source": "Example News",
        "url": url,
        "overall_sentiment": overall,
        "overall_label": "negative",
        "topics": ["Technology"],
        "all_tickers": [
            {
                "ticker": ticker,
                "sentiment": -0.4,
                "relevance": 0.8,
                "label": "negative",
            }
        ],
    }


class CanonicalizeUrlTests(unittest.TestCase):
    def test_removes_tracking_normalizes_host_and_sorts_query(self):
        raw = "HTTPS://www.Example.com/story/?utm_source=x&b=2&a=1#section"

        self.assertEqual(
            canonicalize_url(raw),
            "https://example.com/story?a=1&b=2",
        )


class NormalizeDatasetTests(unittest.TestCase):
    def test_keeps_one_article_and_one_relation_per_ticker(self):
        shared_url = "https://example.com/story"
        ticker_payloads = {
            "AAPL": {"articles": [article(shared_url), article(shared_url)]},
            "MSFT": {
                "articles": [article(shared_url, ticker="MSFT")],
            },
        }

        result = normalize_dataset(ticker_payloads, {}, date(2026, 1, 31))

        self.assertEqual(len(result.articles), 1)
        self.assertEqual(len(result.article_tickers), 2)
        self.assertEqual(
            {(row["ticker"], row["article_weight"]) for row in result.article_tickers},
            {("AAPL", 0.5), ("MSFT", 0.5)},
        )
        self.assertEqual(result.metrics["raw_article_occurrences"], 3)
        self.assertEqual(result.metrics["exact_duplicate_rows_within_ticker"], 1)
        self.assertEqual(result.metrics["cross_ticker_duplicate_occurrences"], 1)

    def test_collapses_urls_that_only_differ_by_tracking_parameters(self):
        ticker_payloads = {
            "AAPL": {
                "articles": [
                    article("https://example.com/story?utm_source=mail"),
                    article("https://www.example.com/story#top"),
                ]
            }
        }

        result = normalize_dataset(ticker_payloads, {}, date(2026, 1, 31))

        self.assertEqual(len(result.articles), 1)
        self.assertEqual(len(result.article_tickers), 1)
        self.assertEqual(result.metrics["canonical_duplicate_rows_within_ticker"], 1)

    def test_does_not_merge_a_dynamic_url_reused_for_another_story(self):
        ticker_payloads = {
            "AAPL": {
                "articles": [
                    article("https://example.com/live", date_="2026-01-10", headline="Day one"),
                    article("https://example.com/live", date_="2026-01-11", headline="Day two"),
                ]
            }
        }

        result = normalize_dataset(ticker_payloads, {}, date(2026, 1, 31))

        self.assertEqual(len(result.articles), 2)
        self.assertEqual(len(result.article_tickers), 2)

    def test_averages_article_level_sentiment_across_ticker_queries(self):
        ticker_payloads = {
            "AAPL": {"articles": [article("https://example.com/story", overall=-0.2)]},
            "MSFT": {
                "articles": [
                    article("https://example.com/story", ticker="MSFT", overall=-0.4)
                ]
            },
        }

        result = normalize_dataset(ticker_payloads, {}, date(2026, 1, 31))

        self.assertAlmostEqual(result.articles[0]["overall_sentiment"], -0.3)
        self.assertEqual(result.articles[0]["overall_label"], "negative")

    def test_uses_spy_sentiment_for_market_index(self):
        ticker_payloads = {
            "^GSPC": {
                "articles": [article("https://example.com/market", ticker="SPY")]
            }
        }

        result = normalize_dataset(ticker_payloads, {}, date(2026, 1, 31))

        self.assertEqual(result.article_tickers[0]["ticker"], "^GSPC")
        self.assertEqual(result.article_tickers[0]["sentiment"], -0.4)
        self.assertEqual(result.metrics["missing_ticker_associations"], 0)

    def test_preserves_all_tracked_ticker_associations_from_the_article(self):
        raw = article("https://example.com/shared")
        raw["all_tickers"].append(
            {
                "ticker": "MSFT",
                "sentiment": 0.3,
                "relevance": 0.7,
                "label": "positive",
            }
        )
        ticker_payloads = {
            "AAPL": {"articles": [raw]},
            "MSFT": {"articles": []},
        }

        result = normalize_dataset(ticker_payloads, {}, date(2026, 1, 31))

        self.assertEqual(
            {row["ticker"] for row in result.article_tickers},
            {"AAPL", "MSFT"},
        )

    def test_rejects_malformed_numeric_research_values(self):
        raw = article("https://example.com/bad")
        raw["all_tickers"][0]["sentiment"] = "not-a-number"

        with self.assertRaisesRegex(ValueError, "sentiment"):
            normalize_dataset(
                {"AAPL": {"articles": [raw]}}, {}, date(2026, 1, 31)
            )

    def test_filters_future_rows_and_deduplicates_prices(self):
        price_payloads = {
            "AAPL": {
                "prices": [
                    {"date": "2026-01-02", "close": 100.0, "return": None},
                    {"date": "2026-01-02", "close": 101.0, "return": 0.01},
                    {"date": "2026-02-02", "close": 102.0, "return": 0.01},
                ]
            }
        }

        result = normalize_dataset({}, price_payloads, date(2026, 1, 31))

        self.assertEqual(
            result.prices,
            [{"ticker": "AAPL", "date": "2026-01-02", "close": 101.0, "daily_return": 0.01}],
        )
        self.assertEqual(result.metrics["duplicate_price_rows"], 1)
        self.assertEqual(result.metrics["future_price_rows_filtered"], 1)

    def test_reports_articles_without_the_file_ticker_association(self):
        ticker_payloads = {
            "AAPL": {
                "articles": [article("https://example.com/story", ticker="MSFT")]
            }
        }

        result = normalize_dataset(ticker_payloads, {}, date(2026, 1, 31))

        self.assertEqual(len(result.articles), 1)
        self.assertEqual(result.article_tickers, [])
        self.assertEqual(result.metrics["missing_ticker_associations"], 1)


class BuildCommandTests(unittest.TestCase):
    def test_build_version_is_pinned_to_date_and_commit(self):
        self.assertEqual(
            build_version(date(2026, 8, 20), "abcdef123456", "1234567890abcdef"),
            "2026-08-20-abcdef1-12345678",
        )

    def test_rejects_unsafe_bigquery_dataset_identifier(self):
        with self.assertRaises(ValueError):
            validate_dataset_id("dataset; DROP TABLE")

    def test_normalized_fingerprint_is_independent_of_input_order(self):
        first = normalize_dataset(
            {
                "MSFT": {"articles": [article("https://example.com/b", ticker="MSFT")]},
                "AAPL": {"articles": [article("https://example.com/a")]},
            },
            {},
            date(2026, 1, 31),
        )
        second = normalize_dataset(
            {
                "AAPL": {"articles": [article("https://example.com/a")]},
                "MSFT": {"articles": [article("https://example.com/b", ticker="MSFT")]},
            },
            {},
            date(2026, 1, 31),
        )

        self.assertEqual(dataset_fingerprint(first), dataset_fingerprint(second))

    def test_fingerprint_is_stable_when_tracking_url_order_changes(self):
        tracked = article("https://www.example.com/story?utm_source=mail")
        canonical = article("https://example.com/story")
        first = normalize_dataset(
            {"AAPL": {"articles": [tracked, canonical]}},
            {},
            date(2026, 1, 31),
        )
        second = normalize_dataset(
            {"AAPL": {"articles": [canonical, tracked]}},
            {},
            date(2026, 1, 31),
        )

        self.assertEqual(dataset_fingerprint(first), dataset_fingerprint(second))

    def test_quality_gate_rejects_out_of_range_relevance(self):
        result = normalize_dataset(
            {"AAPL": {"articles": [article("https://example.com/a")]}},
            {
                "AAPL": {
                    "prices": [
                        {"date": "2026-01-02", "close": 100, "return": None}
                    ]
                }
            },
            date(2026, 1, 31),
        )
        result.article_tickers[0]["relevance"] = 1.1

        with self.assertRaisesRegex(RuntimeError, "relevance"):
            validate_normalized(result, 1)

    def test_existing_identical_bigquery_version_is_a_no_op(self):
        from google.cloud import bigquery

        publisher = object.__new__(BigQueryPublisher)
        publisher.bigquery = bigquery
        publisher.dataset_ref = "project.dataset"
        publisher.client = Mock()
        publisher.client.query.return_value.result.return_value = [
            SimpleNamespace(
                dataset_fingerprint="fingerprint",
                git_commit="abcdef",
                as_of=date(2026, 8, 20),
            )
        ]
        manifest = {
            "dataset_version": "version",
            "dataset_fingerprint": "fingerprint",
            "git_commit": "abcdef",
            "as_of": "2026-08-20",
        }

        self.assertTrue(publisher._assert_version_compatible(manifest))

        manifest["dataset_fingerprint"] = "changed"
        with self.assertRaisesRegex(RuntimeError, "different normalized content"):
            publisher._assert_version_compatible(manifest)


if __name__ == "__main__":
    unittest.main()
