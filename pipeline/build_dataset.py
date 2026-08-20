"""Build a versioned analytical dataset from the raw GCS collection.

The command always rebuilds a complete snapshot up to ``--as-of``. At the
current dataset size this is deliberately simpler and safer than maintaining
incremental transformation state. BigQuery publication uses staging tables and
a transaction, so a failed build never exposes a partial dataset version.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import storage

try:
    from .dataset_builder import NormalizedDataset, normalize_dataset
except ImportError:  # Support ``python pipeline/build_dataset.py``.
    from dataset_builder import NormalizedDataset, normalize_dataset


BASE_DIR = Path(__file__).parent
TICKERS_FILE = BASE_DIR / "tickers.json"
DEFAULT_BUCKET = os.getenv("GCS_BUCKET", "sentiment-mercati-data")
DEFAULT_BQ_DATASET = os.getenv("BQ_DATASET", "sentiment_mercati")
DEFAULT_BQ_LOCATION = os.getenv("BQ_LOCATION", "EU")
DATASET_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


ARTICLE_SCHEMA = [
    ("dataset_version", "STRING", "REQUIRED"),
    ("article_id", "STRING", "REQUIRED"),
    ("date", "DATE", "REQUIRED"),
    ("headline", "STRING", "NULLABLE"),
    ("summary", "STRING", "NULLABLE"),
    ("source", "STRING", "NULLABLE"),
    ("canonical_url", "STRING", "NULLABLE"),
    ("original_url", "STRING", "NULLABLE"),
    ("overall_sentiment", "FLOAT", "NULLABLE"),
    ("overall_label", "STRING", "NULLABLE"),
    ("topics", "STRING", "REPEATED"),
]
ARTICLE_TICKER_SCHEMA = [
    ("dataset_version", "STRING", "REQUIRED"),
    ("article_id", "STRING", "REQUIRED"),
    ("ticker", "STRING", "REQUIRED"),
    ("sentiment", "FLOAT", "REQUIRED"),
    ("relevance", "FLOAT", "REQUIRED"),
    ("label", "STRING", "REQUIRED"),
    ("weighted_sentiment", "FLOAT", "REQUIRED"),
    ("associated_tickers", "INTEGER", "REQUIRED"),
    ("article_weight", "FLOAT", "REQUIRED"),
]
PRICE_SCHEMA = [
    ("dataset_version", "STRING", "REQUIRED"),
    ("ticker", "STRING", "REQUIRED"),
    ("date", "DATE", "REQUIRED"),
    ("close", "FLOAT", "REQUIRED"),
    ("daily_return", "FLOAT", "NULLABLE"),
]
RUN_SCHEMA = [
    ("dataset_version", "STRING", "REQUIRED"),
    ("created_at", "TIMESTAMP", "REQUIRED"),
    ("as_of", "DATE", "REQUIRED"),
    ("git_commit", "STRING", "NULLABLE"),
    ("source_checkpoint", "STRING", "REQUIRED"),
    ("dataset_fingerprint", "STRING", "REQUIRED"),
    ("raw_article_occurrences", "INTEGER", "REQUIRED"),
    ("unique_articles", "INTEGER", "REQUIRED"),
    ("article_ticker_relationships", "INTEGER", "REQUIRED"),
    ("price_rows", "INTEGER", "REQUIRED"),
    ("metrics_json", "STRING", "REQUIRED"),
]


class GcsSnapshotSource:
    """Read one consistent-enough raw snapshot and fingerprint its generations."""

    def __init__(self, bucket_name: str, project: str | None = None):
        self.client = storage.Client(project=project)
        self.bucket = self.client.bucket(bucket_name)
        self.bucket_name = bucket_name

    def read(self, tickers: list[str]) -> tuple[dict, dict, str]:
        expected = {
            *(f"{ticker}/news.json" for ticker in tickers),
            *(f"{ticker}/prices.json" for ticker in tickers),
        }
        metadata = {
            blob.name: blob
            for blob in self.client.list_blobs(self.bucket)
            if blob.name in expected
        }
        missing = sorted(expected - metadata.keys())
        if missing:
            raise RuntimeError(f"Missing {len(missing)} expected GCS blobs: {missing}")

        payloads: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(
                    self._download_json, name, metadata[name].generation
                ): name
                for name in sorted(expected)
            }
            for future in as_completed(futures):
                name = futures[future]
                payloads[name] = future.result()

        fingerprint_input = "\n".join(
            f"{name}:{metadata[name].generation}" for name in sorted(metadata)
        )
        checkpoint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
        news = {ticker: payloads[f"{ticker}/news.json"] for ticker in tickers}
        prices = {ticker: payloads[f"{ticker}/prices.json"] for ticker in tickers}
        return news, prices, checkpoint

    def _download_json(self, blob_name: str, generation: int) -> dict:
        # Pin the listed generation so the fingerprint and downloaded bytes
        # describe the same snapshot even if collection runs concurrently.
        raw = self.bucket.blob(blob_name, generation=generation).download_as_text(
            encoding="utf-8"
        )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a JSON object in gs://{self.bucket_name}/{blob_name}")
        return payload


class BigQueryPublisher:
    """Publish a normalized version atomically behind a stable table interface."""

    def __init__(self, project: str | None, dataset_id: str, location: str):
        validate_dataset_id(dataset_id)
        from google.cloud import bigquery

        self.bigquery = bigquery
        self.client = bigquery.Client(project=project, location=location)
        self.project = self.client.project
        self.dataset_id = dataset_id
        self.location = location
        self.dataset_ref = f"{self.project}.{dataset_id}"

    def publish(
        self,
        dataset: NormalizedDataset,
        manifest: dict[str, Any],
    ) -> bool:
        self._ensure_destination()
        if self._assert_version_compatible(manifest):
            return False
        self._create_current_views()
        suffix = uuid.uuid4().hex[:12]
        staging = {
            "articles": f"{self.dataset_ref}.__staging_articles_{suffix}",
            "article_tickers": f"{self.dataset_ref}.__staging_article_tickers_{suffix}",
            "prices": f"{self.dataset_ref}.__staging_prices_{suffix}",
            "dataset_runs": f"{self.dataset_ref}.__staging_dataset_runs_{suffix}",
        }
        version = manifest["dataset_version"]
        rows = {
            "articles": _with_version(dataset.articles, version),
            "article_tickers": _with_version(dataset.article_tickers, version),
            "prices": _with_version(dataset.prices, version),
            "dataset_runs": [_run_row(manifest)],
        }
        schemas = {
            "articles": ARTICLE_SCHEMA,
            "article_tickers": ARTICLE_TICKER_SCHEMA,
            "prices": PRICE_SCHEMA,
            "dataset_runs": RUN_SCHEMA,
        }

        try:
            for name in staging:
                self._load_staging(staging[name], rows[name], schemas[name])
                actual = self._row_count(staging[name])
                if actual != len(rows[name]):
                    raise RuntimeError(
                        f"Staging validation failed for {name}: "
                        f"expected {len(rows[name])}, got {actual}"
                    )
            self._commit_version(staging, version)
            return True
        finally:
            for table_id in staging.values():
                self.client.delete_table(table_id, not_found_ok=True)

    def _ensure_destination(self) -> None:
        bq = self.bigquery
        dataset = bq.Dataset(self.dataset_ref)
        dataset.location = self.location
        self.client.create_dataset(dataset, exists_ok=True)

        definitions = {
            "articles": (ARTICLE_SCHEMA, "date", ["article_id", "source"]),
            "article_tickers": (ARTICLE_TICKER_SCHEMA, None, ["ticker", "article_id"]),
            "prices": (PRICE_SCHEMA, "date", ["ticker"]),
            "dataset_runs": (RUN_SCHEMA, "created_at", ["dataset_version"]),
        }
        for name, (schema, partition_field, clusters) in definitions.items():
            table = bq.Table(
                f"{self.dataset_ref}.{name}", schema=_bq_schema(bq, schema)
            )
            if partition_field:
                table.time_partitioning = bq.TimePartitioning(field=partition_field)
            table.clustering_fields = clusters
            self.client.create_table(table, exists_ok=True)

    def _load_staging(self, table_id: str, rows: list[dict], schema: list[tuple]) -> None:
        config = self.bigquery.LoadJobConfig(
            schema=_bq_schema(self.bigquery, schema),
            write_disposition=self.bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        self.client.load_table_from_json(rows, table_id, job_config=config).result()

    def _row_count(self, table_id: str) -> int:
        query = f"SELECT COUNT(*) AS n FROM `{table_id}`"
        return int(next(iter(self.client.query(query).result())).n)

    def _commit_version(self, staging: dict[str, str], version: str) -> None:
        bq = self.bigquery
        query = f"""
        BEGIN TRANSACTION;
          DELETE FROM `{self.dataset_ref}.articles` WHERE dataset_version = @version;
          INSERT INTO `{self.dataset_ref}.articles` SELECT * FROM `{staging['articles']}`;

          DELETE FROM `{self.dataset_ref}.article_tickers` WHERE dataset_version = @version;
          INSERT INTO `{self.dataset_ref}.article_tickers` SELECT * FROM `{staging['article_tickers']}`;

          DELETE FROM `{self.dataset_ref}.prices` WHERE dataset_version = @version;
          INSERT INTO `{self.dataset_ref}.prices` SELECT * FROM `{staging['prices']}`;

          DELETE FROM `{self.dataset_ref}.dataset_runs` WHERE dataset_version = @version;
          INSERT INTO `{self.dataset_ref}.dataset_runs` SELECT * FROM `{staging['dataset_runs']}`;
        COMMIT TRANSACTION;
        """
        config = bq.QueryJobConfig(
            query_parameters=[bq.ScalarQueryParameter("version", "STRING", version)]
        )
        self.client.query(query, job_config=config).result()

    def _create_current_views(self) -> None:
        for name in ("articles", "article_tickers", "prices"):
            query = f"""
            CREATE OR REPLACE VIEW `{self.dataset_ref}.current_{name}` AS
            SELECT * EXCEPT(dataset_version)
            FROM `{self.dataset_ref}.{name}`
            WHERE dataset_version = (
              SELECT dataset_version
              FROM `{self.dataset_ref}.dataset_runs`
              ORDER BY created_at DESC
              LIMIT 1
            )
            """
            self.client.query(query).result()

    def _assert_version_compatible(self, manifest: dict[str, Any]) -> bool:
        """Allow idempotent reruns but never mutate an existing version's identity."""
        bq = self.bigquery
        query = f"""
        SELECT dataset_fingerprint, git_commit, as_of
        FROM `{self.dataset_ref}.dataset_runs`
        WHERE dataset_version = @version
        LIMIT 1
        """
        config = bq.QueryJobConfig(
            query_parameters=[
                bq.ScalarQueryParameter(
                    "version", "STRING", manifest["dataset_version"]
                )
            ]
        )
        existing = list(self.client.query(query, job_config=config).result())
        if not existing:
            return False
        row = existing[0]
        expected = (
            manifest["dataset_fingerprint"],
            manifest.get("git_commit"),
            manifest["as_of"],
        )
        actual = (row.dataset_fingerprint, row.git_commit, row.as_of.isoformat())
        if actual != expected:
            raise RuntimeError(
                f"Dataset version {manifest['dataset_version']!r} already exists "
                "with different normalized content, commit, or as-of date"
            )
        return True


def validate_dataset_id(dataset_id: str) -> None:
    if not DATASET_ID_RE.fullmatch(dataset_id):
        raise ValueError(
            "BigQuery dataset id may contain only letters, numbers, and underscores"
        )


def build_version(as_of: date, git_commit: str | None, dataset_fingerprint: str) -> str:
    suffix = (git_commit or "local")[:7]
    version = f"{as_of.isoformat()}-{suffix}-{dataset_fingerprint[:8]}"
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid dataset version: {version!r}")
    return version


def validate_normalized(dataset: NormalizedDataset, expected_tickers: int) -> None:
    if not dataset.articles:
        raise RuntimeError("Quality gate failed: normalized article table is empty")
    if not dataset.article_tickers:
        raise RuntimeError("Quality gate failed: article-ticker table is empty")
    if not dataset.prices:
        raise RuntimeError("Quality gate failed: normalized price table is empty")
    price_tickers = {row["ticker"] for row in dataset.prices}
    if len(price_tickers) != expected_tickers:
        raise RuntimeError(
            f"Quality gate failed: prices cover {len(price_tickers)}/{expected_tickers} tickers"
        )
    relationship_tickers = {row["ticker"] for row in dataset.article_tickers}
    if len(relationship_tickers) != expected_tickers:
        raise RuntimeError(
            "Quality gate failed: article relationships cover "
            f"{len(relationship_tickers)}/{expected_tickers} tickers"
        )
    if dataset.metrics["missing_ticker_associations"]:
        raise RuntimeError(
            "Quality gate failed: "
            f"{dataset.metrics['missing_ticker_associations']} articles do not contain "
            "the sentiment association expected from their ticker file"
        )
    if dataset.metrics["article_conflicts"]:
        raise RuntimeError(
            "Quality gate failed: "
            f"{dataset.metrics['article_conflicts']} normalized article identities "
            "have conflicting core metadata"
        )
    if dataset.metrics["duplicate_price_rows"]:
        raise RuntimeError(
            "Quality gate failed: "
            f"{dataset.metrics['duplicate_price_rows']} duplicate ticker-date price rows "
            "were present in the raw snapshot"
        )
    article_ids = [row["article_id"] for row in dataset.articles]
    if len(article_ids) != len(set(article_ids)):
        raise RuntimeError("Quality gate failed: duplicate article_id values")
    known_articles = set(article_ids)
    relationship_keys = [
        (row["article_id"], row["ticker"]) for row in dataset.article_tickers
    ]
    if len(relationship_keys) != len(set(relationship_keys)):
        raise RuntimeError("Quality gate failed: duplicate article-ticker relationships")
    orphaned = {
        row["article_id"]
        for row in dataset.article_tickers
        if row["article_id"] not in known_articles
    }
    if orphaned:
        raise RuntimeError(
            f"Quality gate failed: {len(orphaned)} article-ticker relationships are orphaned"
        )
    for row in dataset.articles:
        if not -1 <= row["overall_sentiment"] <= 1:
            raise RuntimeError("Quality gate failed: overall sentiment outside [-1, 1]")
    for row in dataset.article_tickers:
        if not -1 <= row["sentiment"] <= 1:
            raise RuntimeError("Quality gate failed: ticker sentiment outside [-1, 1]")
        if not 0 <= row["relevance"] <= 1:
            raise RuntimeError("Quality gate failed: ticker relevance outside [0, 1]")
        if not -1 <= row["weighted_sentiment"] <= 1:
            raise RuntimeError("Quality gate failed: weighted sentiment outside [-1, 1]")
        if row["associated_tickers"] < 1 or not 0 < row["article_weight"] <= 1:
            raise RuntimeError("Quality gate failed: invalid article association weight")
    actual_associations = Counter(
        row["article_id"] for row in dataset.article_tickers
    )
    for row in dataset.article_tickers:
        expected_count = actual_associations[row["article_id"]]
        if row["associated_tickers"] != expected_count or not abs(
            row["article_weight"] - (1 / expected_count)
        ) < 1e-7:
            raise RuntimeError(
                "Quality gate failed: article association counts or weights are inconsistent"
            )
    price_keys = [(row["ticker"], row["date"]) for row in dataset.prices]
    if len(price_keys) != len(set(price_keys)):
        raise RuntimeError("Quality gate failed: duplicate ticker-date price rows")
    if any(row["close"] <= 0 for row in dataset.prices):
        raise RuntimeError("Quality gate failed: non-positive close price")


def _with_version(rows: list[dict], version: str) -> list[dict]:
    return [{"dataset_version": version, **row} for row in rows]


def _run_row(manifest: dict[str, Any]) -> dict:
    metrics = manifest["metrics"]
    return {
        "dataset_version": manifest["dataset_version"],
        "created_at": manifest["created_at"],
        "as_of": manifest["as_of"],
        "git_commit": manifest.get("git_commit"),
        "source_checkpoint": manifest["source_checkpoint"],
        "dataset_fingerprint": manifest["dataset_fingerprint"],
        "raw_article_occurrences": metrics["raw_article_occurrences"],
        "unique_articles": metrics["unique_articles"],
        "article_ticker_relationships": metrics["article_ticker_relationships"],
        "price_rows": metrics["price_rows"],
        "metrics_json": json.dumps(metrics, sort_keys=True),
    }


def _bq_schema(bigquery, fields: list[tuple]) -> list:
    return [bigquery.SchemaField(name, kind, mode=mode) for name, kind, mode in fields]


def _load_tickers() -> list[str]:
    config = json.loads(TICKERS_FILE.read_text(encoding="utf-8"))
    return config["tickers"] + [config["market_index"]]


def dataset_fingerprint(dataset: NormalizedDataset) -> str:
    """Fingerprint only filtered normalized rows, independent of future raw data."""
    digest = hashlib.sha256()
    for table_name, rows in (
        ("articles", dataset.articles),
        ("article_tickers", dataset.article_tickers),
        ("prices", dataset.prices),
    ):
        digest.update(f"{table_name}\n".encode("utf-8"))
        for row in rows:
            encoded = json.dumps(
                row, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            digest.update(encoded.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _write_reports(
    report_dir: Path, manifest: dict[str, Any], dataset: NormalizedDataset
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metrics = manifest["metrics"]
    lines = [
        "# Dataset build quality report",
        "",
        f"- Quality gate: `{manifest['quality_status']}`",
        f"- Dataset version: `{manifest['dataset_version']}`",
        f"- As of: `{manifest['as_of']}`",
        f"- GCS source checkpoint: `{manifest['source_checkpoint']}`",
        f"- Normalized fingerprint: `{manifest['dataset_fingerprint']}`",
        f"- Raw article occurrences: {metrics['raw_article_occurrences']:,}",
        f"- Unique canonical articles: {metrics['unique_articles']:,}",
        f"- Article-ticker relationships: {metrics['article_ticker_relationships']:,}",
        f"- Price rows: {metrics['price_rows']:,}",
        f"- Exact URL duplicates within ticker: {metrics['exact_duplicate_rows_within_ticker']:,}",
        f"- Canonical URL duplicates within ticker: {metrics['canonical_duplicate_rows_within_ticker']:,}",
        f"- Cross-ticker duplicate occurrences: {metrics['cross_ticker_duplicate_occurrences']:,}",
        f"- Candidate same-title/date duplicates: {metrics['candidate_story_duplicate_rows']:,}",
        f"- Missing ticker associations: {metrics['missing_ticker_associations']:,}",
        f"- Reused canonical URLs split into stories: {metrics['reused_canonical_urls']:,}",
        f"- Averaged relationship value conflicts: {metrics['relationship_value_conflicts']:,}",
    ]
    if manifest.get("quality_error"):
        lines.append(f"- Quality error: `{manifest['quality_error']}`")
    (report_dir / "quality-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    relation_counts = Counter(row["ticker"] for row in dataset.article_tickers)
    article_dates = {row["article_id"]: row["date"] for row in dataset.articles}
    news_dates_by_ticker: dict[str, list[str]] = {}
    for row in dataset.article_tickers:
        news_dates_by_ticker.setdefault(row["ticker"], []).append(
            article_dates[row["article_id"]]
        )
    prices_by_ticker: dict[str, list[str]] = {}
    for row in dataset.prices:
        prices_by_ticker.setdefault(row["ticker"], []).append(row["date"])
    with (report_dir / "ticker-coverage.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "ticker",
                "article_relationships",
                "first_article_date",
                "last_article_date",
                "price_rows",
                "first_price_date",
                "last_price_date",
            ],
        )
        writer.writeheader()
        for ticker in sorted(prices_by_ticker):
            dates = prices_by_ticker[ticker]
            news_dates = news_dates_by_ticker.get(ticker, [])
            writer.writerow(
                {
                    "ticker": ticker,
                    "article_relationships": relation_counts[ticker],
                    "first_article_date": min(news_dates) if news_dates else "",
                    "last_article_date": max(news_dates) if news_dates else "",
                    "price_rows": len(dates),
                    "first_price_date": min(dates),
                    "last_price_date": max(dates),
                }
            )


def _write_failure_report(report_dir: Path, details: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "build-manifest.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Dataset build quality report",
        "",
        "- Quality gate: `failed`",
        f"- As of: `{details['as_of']}`",
        f"- GCS source checkpoint: `{details['source_checkpoint']}`",
        f"- Error: `{details['quality_error']}`",
    ]
    (report_dir / "quality-report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True, help="Inclusive snapshot date (YYYY-MM-DD)")
    parser.add_argument("--dataset-version", help="Override the generated immutable version")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--bq-dataset", default=DEFAULT_BQ_DATASET)
    parser.add_argument("--bq-location", default=DEFAULT_BQ_LOCATION)
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT"))
    parser.add_argument("--git-commit", default=os.getenv("GITHUB_SHA"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--report-dir", type=Path, default=Path("artifacts") / "dataset-build"
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    as_of = date.fromisoformat(args.as_of)
    if as_of > date.today():
        raise ValueError("--as-of cannot be in the future")
    validate_dataset_id(args.bq_dataset)

    tickers = _load_tickers()
    print(f"Reading {len(tickers)} tickers from gs://{args.bucket} ...")
    news, prices, checkpoint = GcsSnapshotSource(args.bucket, args.project).read(tickers)
    print("Normalizing articles, ticker relationships, and prices ...")
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        normalized = normalize_dataset(news, prices, as_of)
    except Exception as exc:
        _write_failure_report(
            args.report_dir,
            {
                "quality_status": "failed",
                "quality_error": str(exc),
                "created_at": created_at,
                "as_of": as_of.isoformat(),
                "git_commit": args.git_commit,
                "source_checkpoint": checkpoint,
                "gcs_bucket": args.bucket,
                "bq_dataset": args.bq_dataset,
            },
        )
        raise
    fingerprint = dataset_fingerprint(normalized)
    version = args.dataset_version or build_version(as_of, args.git_commit, fingerprint)
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid dataset version: {version!r}")

    manifest = {
        "dataset_version": version,
        "created_at": created_at,
        "as_of": as_of.isoformat(),
        "git_commit": args.git_commit,
        "source_checkpoint": checkpoint,
        "dataset_fingerprint": fingerprint,
        "gcs_bucket": args.bucket,
        "bq_dataset": args.bq_dataset,
        "metrics": normalized.metrics,
        "quality_status": "passed",
        "quality_error": None,
    }
    try:
        validate_normalized(normalized, len(tickers))
    except Exception as exc:
        manifest["quality_status"] = "failed"
        manifest["quality_error"] = str(exc)
        _write_reports(args.report_dir, manifest, normalized)
        raise
    _write_reports(args.report_dir, manifest, normalized)

    print(json.dumps(manifest, indent=2))
    if args.dry_run:
        print("Dry run complete: BigQuery was not modified.")
        return

    print(f"Publishing {version} to BigQuery dataset {args.bq_dataset} ...")
    published = BigQueryPublisher(args.project, args.bq_dataset, args.bq_location).publish(
        normalized, manifest
    )
    if published:
        print(f"Published dataset version {version}.")
    else:
        print(f"Dataset version {version} already exists unchanged; no-op.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
