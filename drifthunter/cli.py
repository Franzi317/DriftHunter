"""DriftHunter CLI: signals -> study -> report pipeline.

Each stage reads/writes parquet (and a small json) under cfg.data_dir so
reruns are cheap and stages can be re-run independently:

- `signals`: full ingestion (Form 4 + SC 13D) + scoring + exchange filter
  -> data/signals.parquet
- `study`: price fetch + event study + coverage
  -> data/events.parquet, data/coverage.json
- `report`: GO/KILL gate + markdown report
  -> data/report.md
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import click
import pandas as pd

from drifthunter.config import Config, load_config
from drifthunter.ingest.http import EdgarClient
from drifthunter.ingest.tickers import TickerMap
from drifthunter.ingest.insider_datasets import download_quarters, quarter_labels
from drifthunter.ingest.form_index import fetch_quarter
from drifthunter.ingest.header_parse import fetch_header
from drifthunter.scorer.models import Signal, ThirteenDFiling
from drifthunter.scorer import form4 as form4_scorer
from drifthunter.scorer import sc13d as sc13d_scorer
from drifthunter.prices.provider import CachingProvider, coverage_report
from drifthunter.prices.free import FreeProvider
from drifthunter.prices.sharadar import SharadarProvider
from drifthunter.backtest.event_study import run_event_study
from drifthunter.backtest.report import evaluate_gate, render_markdown


@click.group()
@click.option("--config", "config_path", default="config.yaml",
               type=click.Path(), help="Path to config.yaml")
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """DriftHunter Phase 0 backtest pipeline."""
    ctx.obj = load_config(Path(config_path))


def _parse_quarter_label(label: str) -> tuple[int, int]:
    """'2021q2' -> (2021, 2)."""
    year_s, q_s = label.split("q")
    return int(year_s), int(q_s)


def _make_provider(cfg: Config) -> CachingProvider:
    """Construct the configured price provider, wrapped in a disk cache."""
    if cfg.prices.provider == "sharadar":
        import os
        api_key = os.environ.get(cfg.prices.nasdaq_api_key_env)
        if not api_key:
            raise click.ClickException(
                f"Environment variable {cfg.prices.nasdaq_api_key_env} is not set "
                f"(required for prices.provider: sharadar). Set it, or set "
                f"prices.provider: free in config.yaml to use the free fallback."
            )
        inner = SharadarProvider(api_key)
    elif cfg.prices.provider == "free":
        inner = FreeProvider()
    else:
        raise click.ClickException(
            f"Unknown prices.provider: {cfg.prices.provider!r} (expected 'sharadar' or 'free')"
        )

    return CachingProvider(inner, cfg.data_dir / "prices")


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_obj
def signals(cfg: Config) -> None:
    """Ingest Form 4 + SC 13D filings, score, and filter to tradable signals.

    Resumability is cache-backed (EdgarClient disk cache under
    data/edgar_cache), not checkpoint-backed: clearing the cache directory
    re-fetches everything.
    """
    start = date.fromisoformat(cfg.study.start)
    end = date.fromisoformat(cfg.study.end)

    cache_dir = cfg.data_dir / "edgar_cache"
    client = EdgarClient(cfg.edgar, cache_dir)
    tickers = TickerMap.fetch(client)

    # --- Form 4 ---
    buys = download_quarters(client, start, end, cfg.form4)
    form4_signals = form4_scorer.detect_signals(buys, cfg.form4)

    # --- SC 13D ---
    filings: list[ThirteenDFiling] = []
    n_blank = 0
    for label in quarter_labels(start, end):
        year, q = _parse_quarter_label(label)
        rows = fetch_quarter(client, year, q)
        for row in rows:
            if not (start <= row.date_filed <= end):
                continue
            hdr = fetch_header(client, row.path)
            if not hdr.subject_cik:
                n_blank += 1
            filings.append(ThirteenDFiling(
                accession=hdr.accession,
                subject_cik=hdr.subject_cik,
                subject_name=hdr.subject_name,
                ticker=tickers.ticker_for_cik(hdr.subject_cik) if hdr.subject_cik else None,
                filer_cik=hdr.filer_cik,
                filer_name=hdr.filer_name,
                filing_date=row.date_filed,
                is_amendment=False,
            ))
    click.echo(f"sc13d: {n_blank} headers with blank subject CIK (skipped ticker mapping)")

    sc13d_signals = sc13d_scorer.detect_signals(filings, cfg.sc13d)

    # --- Exchange filter (spec section 3.3) ---
    ticker_to_cik: dict[str, str] = {}
    for b in buys:
        if b.ticker:
            ticker_to_cik[b.ticker] = b.issuer_cik
    for f in filings:
        if f.ticker:
            ticker_to_cik[f.ticker] = f.subject_cik

    kept: list[Signal] = []
    for profile, profile_signals in (("form4", form4_signals), ("sc13d", sc13d_signals)):
        n_kept = 0
        n_dropped = 0
        for sig in profile_signals:
            cik = ticker_to_cik.get(sig.ticker)
            if cik is not None and tickers.is_listed(cik, cfg.tradability.allowed_exchanges):
                kept.append(sig)
                n_kept += 1
            else:
                n_dropped += 1
        click.echo(f"{profile}: kept {n_kept}, dropped {n_dropped} (exchange filter)")

    df = pd.DataFrame({
        "profile": [s.profile for s in kept],
        "ticker": [s.ticker for s in kept],
        "trigger_date": [pd.Timestamp(s.trigger_date) for s in kept],
        "score": [s.score for s in kept],
        "detail": [s.detail for s in kept],
    })

    out_path = cfg.data_dir / "signals.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path)
    click.echo(f"wrote {len(df)} signals to {out_path}")


# ---------------------------------------------------------------------------
# study
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_obj
def study(cfg: Config) -> None:
    """Fetch prices and run the event study + coverage report."""
    signals_path = cfg.data_dir / "signals.parquet"
    df = pd.read_parquet(signals_path)
    sigs = [
        Signal(
            profile=row.profile,
            ticker=row.ticker,
            trigger_date=row.trigger_date.date(),
            score=row.score,
            detail=row.detail,
        )
        for row in df.itertuples()
    ]

    provider = _make_provider(cfg)

    events = run_event_study(
        sigs, provider, cfg.tradability,
        horizons=cfg.study.horizons,
        cost_bps_list=cfg.study.cost_bps_sweep,
        benchmark=cfg.study.benchmark,
    )
    events_path = cfg.data_dir / "events.parquet"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events.to_parquet(events_path)
    click.echo(f"wrote {len(events)} event rows to {events_path}")

    study_start = date.fromisoformat(cfg.study.start)
    study_end = date.fromisoformat(cfg.study.end)

    coverage: dict[str, dict] = {}
    for profile, group in df.groupby("profile"):
        rep = coverage_report(group["ticker"].tolist(), provider, study_start, study_end)
        coverage[profile] = {
            "total": rep.total,
            "covered": rep.covered,
            "missing": rep.missing,
            "rate": rep.rate,
        }
        click.echo(f"{profile}: coverage {rep.covered}/{rep.total} ({rep.rate:.1%})")

    coverage_path = cfg.data_dir / "coverage.json"
    coverage_path.write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    click.echo(f"wrote coverage to {coverage_path}")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_obj
def report(cfg: Config) -> None:
    """Evaluate the GO/KILL gate and render the markdown report."""
    events_path = cfg.data_dir / "events.parquet"
    coverage_path = cfg.data_dir / "coverage.json"
    events = pd.read_parquet(events_path)
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

    provider = _make_provider(cfg)

    verdicts = []
    for profile in coverage:
        completed = events[
            (events["profile"] == profile) & (events["filter_reason"] == "")
        ]
        spy_annual = 0.0
        if not completed.empty:
            entry_min = pd.Timestamp(completed["entry_date"].min())
            exit_max = pd.Timestamp(completed["exit_date"].max())
            span_years = (exit_max - entry_min).days / 365.25
            spy = provider.daily(cfg.study.benchmark, entry_min.date(), exit_max.date())
            if not spy.empty and span_years > 0:
                first_close = float(spy["close"].iloc[0])
                last_close = float(spy["close"].iloc[-1])
                spy_annual = (last_close / first_close) ** (1.0 / span_years) - 1.0
            else:
                click.echo(
                    f"{profile}: WARNING no SPY span data; portfolio-excess check degraded "
                    f"(spy_annual_return=0.0)"
                )
        else:
            click.echo(
                f"{profile}: WARNING no completed events; portfolio-excess check degraded "
                f"(spy_annual_return=0.0)"
            )

        verdict = evaluate_gate(
            events, coverage_rate=coverage[profile]["rate"], profile=profile,
            gate=cfg.gate, port=cfg.portfolio,
            headline_cost_bps=cfg.study.headline_cost_bps,
            horizons=cfg.study.horizons, seed=cfg.study.seed,
            spy_annual_return=spy_annual,
            bootstrap_iterations=cfg.study.bootstrap_iterations,
        )
        verdicts.append(verdict)
        click.echo(f"{profile}: {verdict.decision} ({'; '.join(verdict.reasons)})")

    md = render_markdown(verdicts, events)
    report_path = cfg.data_dir / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(md, encoding="utf-8")
    click.echo(f"wrote report to {report_path}")
