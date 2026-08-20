"""Run the reproducible market/news analysis against a BigQuery snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy
import scipy
import statsmodels

try:
    from .analysis_engine import AnalysisResult, analyze_market_data
except ImportError:  # Support ``python pipeline/analyze_dataset.py``.
    from analysis_engine import AnalysisResult, analyze_market_data


DEFAULT_BQ_DATASET = os.getenv("BQ_DATASET", "sentiment_mercati")
DEFAULT_BQ_LOCATION = os.getenv("BQ_LOCATION", "EU")
DATASET_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

CSV_FIELDS = {
    "daily-series.csv": [
        "ticker",
        "date",
        "close",
        "daily_return",
        "sentiment",
        "article_count",
    ],
    "pearson.csv": [
        "ticker",
        "lag",
        "direction",
        "correlation",
        "pvalue",
        "qvalue",
        "global_qvalue",
        "significant",
        "significant_fdr",
        "significant_global_fdr",
        "n",
    ],
    "granger.csv": [
        "ticker",
        "direction",
        "lag",
        "f_stat",
        "pvalue",
        "qvalue",
        "global_qvalue",
        "significant_fdr",
        "significant_global_fdr",
    ],
    "downside-events.csv": [
        "ticker",
        "date",
        "daily_return",
        "event_threshold",
        "pre_sentiment",
        "post_sentiment",
        "sentiment_change",
        "pre_articles",
        "post_articles",
        "window_sessions",
    ],
    "ticker-summary.csv": [
        "ticker",
        "observations",
        "news_sessions",
        "articles",
        "start_date",
        "end_date",
        "granger_conclusion",
        "granger_global_conclusion",
        "granger_news_to_market_status",
        "granger_market_to_news_status",
        "adf_pvalue_sentiment",
        "adf_pvalue_returns",
        "downside_events",
        "mean_downside_pre_sentiment",
        "mean_downside_post_sentiment",
        "mean_downside_sentiment_change",
        "downside_pvalue",
    ],
}


class BigQueryAnalysisSource:
    """Read one immutable normalized version through a narrow adapter."""

    def __init__(self, project: str | None, dataset_id: str, location: str):
        if not DATASET_ID_RE.fullmatch(dataset_id):
            raise ValueError("BigQuery dataset id contains invalid characters")
        from google.cloud import bigquery

        self.bigquery = bigquery
        self.client = bigquery.Client(project=project, location=location)
        self.dataset_ref = f"{self.client.project}.{dataset_id}"

    def read(
        self, dataset_version: str | None = None
    ) -> tuple[dict[str, Any], list[dict], list[dict], list[dict]]:
        if dataset_version and not VERSION_RE.fullmatch(dataset_version):
            raise ValueError("Invalid dataset version")
        run = self._resolve_run(dataset_version)
        version = run["dataset_version"]
        parameters = [
            self.bigquery.ScalarQueryParameter("version", "STRING", version)
        ]
        articles = self._rows(
            f"""
            SELECT article_id, date
            FROM `{self.dataset_ref}.articles`
            WHERE dataset_version = @version
            """,
            parameters,
        )
        article_tickers = self._rows(
            f"""
            SELECT article_id, ticker, weighted_sentiment
            FROM `{self.dataset_ref}.article_tickers`
            WHERE dataset_version = @version
            """,
            parameters,
        )
        prices = self._rows(
            f"""
            SELECT ticker, date, close, daily_return
            FROM `{self.dataset_ref}.prices`
            WHERE dataset_version = @version
            """,
            parameters,
        )
        if not articles or not article_tickers or not prices:
            raise RuntimeError(f"Dataset version {version!r} is incomplete")
        return run, articles, article_tickers, prices

    def _resolve_run(self, dataset_version: str | None) -> dict[str, Any]:
        where = "WHERE dataset_version = @version" if dataset_version else ""
        query = f"""
        SELECT dataset_version, created_at, as_of, git_commit,
               source_checkpoint, dataset_fingerprint
        FROM `{self.dataset_ref}.dataset_runs`
        {where}
        ORDER BY created_at DESC
        LIMIT 1
        """
        parameters = (
            [self.bigquery.ScalarQueryParameter("version", "STRING", dataset_version)]
            if dataset_version
            else []
        )
        rows = self._rows(query, parameters)
        if not rows:
            requested = dataset_version or "current"
            raise RuntimeError(f"Dataset version {requested!r} was not found")
        return rows[0]

    def _rows(self, query: str, parameters: list[Any]) -> list[dict[str, Any]]:
        config = self.bigquery.QueryJobConfig(query_parameters=parameters)
        return [dict(row.items()) for row in self.client.query(query, job_config=config).result()]


def write_analysis_reports(
    output_dir: Path, manifest: dict[str, Any], result: AnalysisResult
) -> None:
    """Write the complete portable result bundle used by humans and machines."""
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(result)
    full_manifest = {
        **manifest,
        "metrics": {
            "daily_rows": len(result.daily_series),
            "pearson_tests": len(result.pearson),
            "granger_tests": len(result.granger),
            "downside_events": len(result.downside_events),
            "tickers": len(result.ticker_summary),
        },
    }
    (output_dir / "analysis-manifest.json").write_text(
        json.dumps(full_manifest, indent=2, default=_json_default), encoding="utf-8"
    )
    (output_dir / "analysis.json").write_text(
        json.dumps(
            {"manifest": full_manifest, **payload},
            indent=2,
            ensure_ascii=False,
            default=_json_default,
        ),
        encoding="utf-8",
    )

    tables = {
        "daily-series.csv": result.daily_series,
        "pearson.csv": result.pearson,
        "granger.csv": result.granger,
        "downside-events.csv": result.downside_events,
        "ticker-summary.csv": result.ticker_summary,
    }
    for filename, rows in tables.items():
        with (output_dir / filename).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS[filename])
            writer.writeheader()
            writer.writerows(rows)

    (output_dir / "report.md").write_text(
        _render_report(full_manifest, result), encoding="utf-8"
    )


def _render_report(manifest: dict[str, Any], result: AnalysisResult) -> str:
    primary = manifest["primary_ticker"]
    summary = next(
        (row for row in result.ticker_summary if row["ticker"] == primary), None
    )
    if summary is None:
        raise RuntimeError(f"Primary ticker {primary!r} is absent from the analysis")

    primary_granger = [row for row in result.granger if row["ticker"] == primary]
    primary_pearson = [row for row in result.pearson if row["ticker"] == primary]
    significant_news = [
        row
        for row in primary_granger
        if row["direction"] == "news_to_market" and row["significant_fdr"]
    ]
    significant_market = [
        row
        for row in primary_granger
        if row["direction"] == "market_to_news" and row["significant_fdr"]
    ]
    primary_events = [row for row in result.downside_events if row["ticker"] == primary]
    conclusion = _conclusion_text(summary["granger_conclusion"])
    counts = Counter(
        row["granger_global_conclusion"] for row in result.ticker_summary
    )

    lines = [
        "# Analisi news e mercato",
        "",
        "## Snapshot",
        "",
        f"- Dataset: `{manifest['dataset_version']}`",
        f"- Fingerprint: `{manifest['dataset_fingerprint']}`",
        f"- Dati inclusi fino al: `{manifest['dataset_as_of']}`",
        f"- Ticker analizzati: {len(result.ticker_summary)}",
        f"- Lag massimi: {manifest['max_lag']} sedute",
        f"- Soglia: {manifest['alpha']}; correzione Benjamini-Hochberg entro ticker",
        f"- Ambiente: Python {manifest.get('environment', {}).get('python', 'n/d')}; "
        f"NumPy {manifest.get('environment', {}).get('numpy', 'n/d')}; "
        f"SciPy {manifest.get('environment', {}).get('scipy', 'n/d')}; "
        f"statsmodels {manifest.get('environment', {}).get('statsmodels', 'n/d')}",
        "",
        f"## Risultato principale — {primary}",
        "",
        conclusion,
        "",
        f"Sedute: {summary['observations']}; sedute con news: "
        f"{summary['news_sessions']}; articoli associati: {summary['articles']}.",
        "",
        _format_granger(
            "News → mercato",
            significant_news,
            summary["granger_news_to_market_status"],
        ),
        _format_granger(
            "Mercato → news",
            significant_market,
            summary["granger_market_to_news_status"],
        ),
        "",
        "### Correlazione Pearson",
        "",
        _format_pearson(primary_pearson),
        "",
        "### Diagnostica di stazionarietà",
        "",
        _format_adf(summary, manifest["alpha"]),
        "",
        "### Event study dei ribassi estremi",
        "",
        _format_event_study(primary_events, summary),
        "",
        "## Risultati trasversali (FDR globale)",
        "",
        f"- News → mercato: {counts['news_to_market']} ticker",
        f"- Mercato → news: {counts['market_to_news']} ticker",
        f"- Bidirezionale: {counts['bidirectional']} ticker",
        f"- Nessuna direzione significativa: {counts['none']} ticker",
        f"- Dati insufficienti: {counts['insufficient_data']} ticker",
        f"- Diagnostica non valida: {counts['invalid_diagnostics']} ticker",
        "",
        "## Metodo",
        "",
        "Il sentiment giornaliero è la media di `sentiment × relevance` per ticker. "
        "Le news di weekend o festività sono assegnate alla prima seduta successiva; "
        "una seduta senza news riceve valore zero. Un lag positivo confronta il "
        "sentiment di oggi con il rendimento futuro.",
        "",
        "I test di Granger vengono eseguiti nelle due direzioni sui rendimenti e sul "
        "sentiment giornaliero. I q-value controllano il false discovery rate entro "
        "ticker; i risultati trasversali usano anche una correzione globale su tutti "
        "i ticker. I ribassi estremi devono essere sia nel 5° percentile del ticker "
        "sia pari o inferiori al −2%; eventi sovrapposti vengono accorpati.",
        "",
        "## Limiti interpretativi",
        "",
        "- Granger misura capacità predittiva condizionale, non causalità economica.",
        "- Le news conservano solo la data, non l'orario: non possiamo distinguere "
        "una notizia pre-market da una pubblicata dopo la chiusura nella stessa seduta.",
        "- La finestra disponibile è ancora breve; i risultati vanno aggiornati con "
        "nuove snapshot e verificati su periodi di mercato differenti.",
        "- L'event study è descrittivo e non elimina fattori comuni o dipendenze tra ticker.",
        "",
    ]
    return "\n".join(lines)


def _conclusion_text(conclusion: str) -> str:
    return {
        "news_to_market": (
            "Dopo la correzione per test multipli, il sentiment delle news aggiunge "
            "capacità predittiva per i rendimenti futuri; non emerge la direzione opposta."
        ),
        "market_to_news": (
            "Dopo la correzione per test multipli, i rendimenti aggiungono capacità "
            "predittiva per il sentiment futuro; non emerge che le news anticipino il mercato."
        ),
        "bidirectional": (
            "Emergono capacità predittiva in entrambe le direzioni; i dati non consentono "
            "una risposta unidirezionale."
        ),
        "none": (
            "Non emerge capacità predittiva statisticamente significativa in nessuna "
            "direzione dopo la correzione per test multipli."
        ),
        "insufficient_data": "I dati sono insufficienti per una conclusione Granger.",
        "invalid_diagnostics": (
            "Non viene emessa una conclusione Granger: la stazionarietà non è verificata "
            "oppure almeno una direzione del test non è risultata calcolabile."
        ),
    }[conclusion]


def _format_granger(
    label: str, rows: list[dict[str, Any]], status: str
) -> str:
    if status != "completed":
        return f"- **{label}:** test non eseguito/non calcolabile (`{status}`)."
    if not rows:
        return f"- **{label}:** nessun lag significativo dopo correzione FDR."
    details = ", ".join(
        f"lag {row['lag']} (q={row['qvalue']:.4g})" for row in rows
    )
    return f"- **{label}:** {details}."


def _format_pearson(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Serie senza variabilità sufficiente per il calcolo."
    strongest = max(rows, key=lambda row: abs(row["correlation"]))
    significance = (
        "significativo dopo FDR entro ticker"
        if strongest["significant_fdr"]
        else "non significativo dopo FDR entro ticker"
    )
    significant_rows = [row for row in rows if row["significant_fdr"]]
    significant_text = (
        " Lag significativi: "
        + ", ".join(
            f"{row['lag']:+d} (r={row['correlation']:+.4f}, q={row['qvalue']:.4g})"
            for row in significant_rows
        )
        + "."
        if significant_rows
        else " Nessun lag significativo dopo FDR entro ticker."
    )
    return (
        f"Massimo valore assoluto: lag {strongest['lag']:+d}, "
        f"r={strongest['correlation']:+.4f}, q={strongest['qvalue']:.4g} "
        f"({significance}).{significant_text}"
    )


def _format_adf(summary: dict[str, Any], alpha: float) -> str:
    sentiment = summary["adf_pvalue_sentiment"]
    returns = summary["adf_pvalue_returns"]

    def value_text(value: float | None) -> str:
        return "non disponibile" if value is None else f"p={value:.4g}"

    warnings_text = []
    if sentiment is not None and sentiment >= alpha:
        warnings_text.append("sentiment non stazionario alla soglia scelta")
    if returns is not None and returns >= alpha:
        warnings_text.append("rendimenti non stazionari alla soglia scelta")
    suffix = (
        " Attenzione: " + "; ".join(warnings_text) + "." if warnings_text else ""
    )
    return (
        f"Test ADF: sentiment {value_text(sentiment)}; rendimenti "
        f"{value_text(returns)}.{suffix}"
    )


def _format_event_study(
    events: list[dict[str, Any]], summary: dict[str, Any]
) -> str:
    if not events:
        return "Nessun evento con una finestra completa disponibile."
    mean_pre = summary["mean_downside_pre_sentiment"]
    mean_post = summary["mean_downside_post_sentiment"]
    change = summary["mean_downside_sentiment_change"]
    pvalue = summary["downside_pvalue"]
    pvalue_text = f"; test appaiato p={pvalue:.4g}" if pvalue is not None else ""
    return (
        f"Eventi: {len(events)}. Sentiment medio nelle 5 sedute precedenti: "
        f"{mean_pre:+.4f}; dalla seduta del ribasso alle 4 successive: "
        f"{mean_post:+.4f}; variazione: {change:+.4f}{pvalue_text}."
    )


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def runtime_environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "numpy": numpy.__version__,
        "scipy": scipy.__version__,
        "statsmodels": statsmodels.__version__,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project")
    parser.add_argument("--bq-dataset", default=DEFAULT_BQ_DATASET)
    parser.add_argument("--bq-location", default=DEFAULT_BQ_LOCATION)
    parser.add_argument("--dataset-version")
    parser.add_argument("--primary-ticker", default="^GSPC")
    parser.add_argument("--max-lag", type=int, default=5)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--git-commit")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("artifacts") / "analysis"
    )
    args = parser.parse_args()

    source = BigQueryAnalysisSource(args.project, args.bq_dataset, args.bq_location)
    run, articles, article_tickers, prices = source.read(args.dataset_version)
    print(f"Analyzing BigQuery dataset version {run['dataset_version']} ...")
    result = analyze_market_data(
        articles,
        article_tickers,
        prices,
        max_lag=args.max_lag,
        alpha=args.alpha,
    )
    if args.primary_ticker not in {row["ticker"] for row in result.ticker_summary}:
        raise RuntimeError(f"Primary ticker {args.primary_ticker!r} has no analysis rows")

    manifest = {
        "dataset_version": run["dataset_version"],
        "dataset_fingerprint": run["dataset_fingerprint"],
        "dataset_as_of": run["as_of"],
        "dataset_git_commit": run.get("git_commit"),
        "analysis_created_at": datetime.now(timezone.utc).isoformat(),
        "analysis_git_commit": args.git_commit,
        "max_lag": args.max_lag,
        "alpha": args.alpha,
        "primary_ticker": args.primary_ticker,
        "bq_dataset": args.bq_dataset,
        "environment": runtime_environment(),
    }
    write_analysis_reports(args.output_dir, manifest, result)
    primary = next(
        row for row in result.ticker_summary if row["ticker"] == args.primary_ticker
    )
    print(json.dumps(manifest, indent=2, default=_json_default))
    print(f"Primary conclusion: {primary['granger_conclusion']}")
    print(f"Reports written to {args.output_dir}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
