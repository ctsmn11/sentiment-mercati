import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from pipeline.analysis_engine import analyze_market_data
from pipeline.analyze_dataset import runtime_environment, write_analysis_reports


class AnalyzeMarketDataTests(unittest.TestCase):
    def test_maps_non_trading_news_to_next_session_and_uses_zero_without_news(self):
        articles = [
            {"article_id": "weekend", "date": "2026-01-03"},
            {"article_id": "monday", "date": "2026-01-05"},
        ]
        article_tickers = [
            {
                "article_id": "weekend",
                "ticker": "AAPL",
                "weighted_sentiment": -0.6,
            },
            {
                "article_id": "monday",
                "ticker": "AAPL",
                "weighted_sentiment": 0.2,
            },
        ]
        prices = [
            {"ticker": "AAPL", "date": "2026-01-02", "close": 100.0, "daily_return": None},
            {"ticker": "AAPL", "date": "2026-01-05", "close": 99.0, "daily_return": -0.01},
            {"ticker": "AAPL", "date": "2026-01-06", "close": 100.0, "daily_return": 0.010101},
        ]

        result = analyze_market_data(
            articles, article_tickers, prices, max_lag=1, alpha=0.05
        )

        daily = result.daily_series
        self.assertEqual([row["date"] for row in daily], ["2026-01-05", "2026-01-06"])
        self.assertAlmostEqual(daily[0]["sentiment"], -0.2)
        self.assertEqual(daily[0]["article_count"], 2)
        self.assertEqual(daily[1]["sentiment"], 0.0)
        self.assertEqual(daily[1]["article_count"], 0)

    def test_positive_pearson_lag_means_news_precede_future_returns(self):
        start = date(2026, 1, 1)
        signals = [float((index % 7) - 3) / 3 for index in range(24)]
        articles = []
        article_tickers = []
        prices = []
        for index, signal in enumerate(signals):
            day = (start + timedelta(days=index)).isoformat()
            article_id = f"a{index}"
            articles.append({"article_id": article_id, "date": day})
            article_tickers.append(
                {
                    "article_id": article_id,
                    "ticker": "TEST",
                    "weighted_sentiment": signal,
                }
            )
            prices.append(
                {
                    "ticker": "TEST",
                    "date": day,
                    "close": 100.0,
                    "daily_return": 0.0 if index == 0 else signals[index - 1],
                }
            )

        result = analyze_market_data(
            articles, article_tickers, prices, max_lag=2, alpha=0.05
        )

        lag_one = next(row for row in result.pearson if row["lag"] == 1)
        self.assertAlmostEqual(lag_one["correlation"], 1.0)
        self.assertEqual(lag_one["direction"], "news_to_market")
        self.assertTrue(lag_one["significant_fdr"])
        self.assertEqual(
            result.ticker_summary[0]["granger_conclusion"], "invalid_diagnostics"
        )
        self.assertIn(
            "infeasible",
            {
                result.ticker_summary[0]["granger_news_to_market_status"],
                result.ticker_summary[0]["granger_market_to_news_status"],
            },
        )

    def test_granger_results_use_fdr_and_summarize_the_direction(self):
        rng = np.random.default_rng(42)
        observations = 240
        sentiment = rng.normal(0, 0.25, observations)
        returns = np.zeros(observations)
        for index in range(1, observations):
            returns[index] = 0.8 * sentiment[index - 1] + rng.normal(0, 0.05)

        start = date(2025, 1, 1)
        articles = []
        relations = []
        prices = []
        for index in range(observations):
            day = (start + timedelta(days=index)).isoformat()
            article_id = f"signal-{index}"
            articles.append({"article_id": article_id, "date": day})
            relations.append(
                {
                    "article_id": article_id,
                    "ticker": "TEST",
                    "weighted_sentiment": float(sentiment[index]),
                }
            )
            prices.append(
                {
                    "ticker": "TEST",
                    "date": day,
                    "close": 100.0,
                    "daily_return": float(returns[index]),
                }
            )

        result = analyze_market_data(
            articles, relations, prices, max_lag=1, alpha=0.05
        )

        news_lag_one = next(
            row
            for row in result.granger
            if row["direction"] == "news_to_market" and row["lag"] == 1
        )
        self.assertLess(news_lag_one["qvalue"], 0.05)
        self.assertTrue(news_lag_one["significant_fdr"])
        self.assertEqual(result.ticker_summary[0]["granger_conclusion"], "news_to_market")

    def test_downside_event_study_compares_equal_pre_and_post_windows(self):
        start = date(2026, 1, 1)
        articles = []
        relations = []
        prices = []
        for index in range(30):
            day = (start + timedelta(days=index)).isoformat()
            article_id = f"event-{index}"
            signal = 0.2 if 10 <= index < 15 else -0.5 if 15 <= index < 20 else 0.0
            articles.append({"article_id": article_id, "date": day})
            relations.append(
                {
                    "article_id": article_id,
                    "ticker": "TEST",
                    "weighted_sentiment": signal,
                }
            )
            prices.append(
                {
                    "ticker": "TEST",
                    "date": day,
                    "close": 100.0,
                    "daily_return": -0.1 if index == 15 else 0.001 + index / 10000,
                }
            )

        result = analyze_market_data(
            articles, relations, prices, max_lag=1, alpha=0.05
        )

        event = result.downside_events[0]
        self.assertEqual(event["date"], "2026-01-16")
        self.assertAlmostEqual(event["pre_sentiment"], 0.2)
        self.assertAlmostEqual(event["post_sentiment"], -0.5)
        self.assertAlmostEqual(event["sentiment_change"], -0.7)
        self.assertLessEqual(event["event_threshold"], -0.02)
        summary = result.ticker_summary[0]
        self.assertAlmostEqual(summary["mean_downside_pre_sentiment"], 0.2)
        self.assertAlmostEqual(summary["mean_downside_post_sentiment"], -0.5)
        self.assertIsNone(summary["downside_pvalue"])

    def test_calm_sample_does_not_manufacture_downside_events(self):
        start = date(2026, 1, 1)
        prices = [
            {
                "ticker": "CALM",
                "date": (start + timedelta(days=index)).isoformat(),
                "close": 100.0,
                "daily_return": 0.001 + index / 100000,
            }
            for index in range(30)
        ]

        result = analyze_market_data([], [], prices, max_lag=1, alpha=0.05)

        self.assertEqual(result.downside_events, [])

    def test_report_artifacts_are_versioned_and_state_causal_limitations(self):
        articles = [{"article_id": "a", "date": "2026-01-02"}]
        relations = [
            {"article_id": "a", "ticker": "^GSPC", "weighted_sentiment": -0.2}
        ]
        prices = [
            {
                "ticker": "^GSPC",
                "date": f"2026-01-{day:02d}",
                "close": 100.0 + day,
                "daily_return": day / 1000,
            }
            for day in range(2, 22)
        ]
        result = analyze_market_data(
            articles, relations, prices, max_lag=1, alpha=0.05
        )
        self.assertEqual(
            result.ticker_summary[0]["granger_conclusion"], "invalid_diagnostics"
        )
        self.assertEqual(result.granger, [])
        manifest = {
            "dataset_version": "snapshot-v1",
            "dataset_fingerprint": "abc123",
            "dataset_as_of": "2026-01-21",
            "analysis_created_at": "2026-01-22T00:00:00+00:00",
            "analysis_git_commit": "deadbeef",
            "max_lag": 1,
            "alpha": 0.05,
            "primary_ticker": "^GSPC",
        }

        with TemporaryDirectory() as directory:
            output = Path(directory)
            write_analysis_reports(output, manifest, result)

            expected = {
                "analysis-manifest.json",
                "analysis.json",
                "report.md",
                "daily-series.csv",
                "pearson.csv",
                "granger.csv",
                "downside-events.csv",
                "ticker-summary.csv",
            }
            self.assertEqual({path.name for path in output.iterdir()}, expected)
            report = (output / "report.md").read_text(encoding="utf-8")
            self.assertIn("snapshot-v1", report)
            self.assertIn("capacità predittiva", report)
            self.assertIn("solo la data", report)
            self.assertIn("Correlazione Pearson", report)
            self.assertIn("ADF", report)
            self.assertIn("test non eseguito", report)

    def test_runtime_environment_records_statistical_versions(self):
        environment = runtime_environment()

        self.assertEqual(
            set(environment), {"python", "numpy", "scipy", "statsmodels"}
        )
        self.assertTrue(all(environment.values()))


if __name__ == "__main__":
    unittest.main()
