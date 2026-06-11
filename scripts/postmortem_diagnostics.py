"""Post-mortem diagnostics for the Phase 0 KILL verdict (both profiles).

This script does NOT modify engine code, gate logic, or data/report.md. It
re-derives prices through the same provider/cache stack as `drifthunter
study` and runs two standalone diagnostics:

Diagnostic A -- the missed day-0 pop
    For each signal, did the documented filing reaction happen BEFORE our
    entry (next open after trigger_date)? Computes the filing-day return
    (last pre-filing close -> trigger-date close, when trigger_date is a
    trading day) and the "missed_total" return (last pre-filing close ->
    our actual entry open), both raw and SPY-adjusted.

Diagnostic B -- IWM benchmark decomposition
    Re-runs the Phase 0 event study with benchmark="IWM" instead of "SPY"
    and compares per-horizon excess returns (at headline cost 30bp) against
    the SPY numbers already recorded in data/events.parquet.

POST-MORTEM DIAGNOSTIC ONLY -- the Phase 0 verdict (KILL vs SPY per spec
Sec3.6) is unaffected by anything in this script.

Outputs: data/diagnostics.md (+ a summary printed to stdout).

Usage:
    uv run python scripts/postmortem_diagnostics.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Sharadar key comes from the environment. Cached price reads never touch
# the network, so a re-run against a warm cache works without any key.
if not os.environ.get("NASDAQ_DATA_LINK_API_KEY"):
    print("NOTE: NASDAQ_DATA_LINK_API_KEY not set; relying on the local "
          "price cache only.", file=sys.stderr)

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from drifthunter.config import load_config
from drifthunter.cli import BenchmarkRoutingProvider, _make_provider
from drifthunter.prices.provider import CachingProvider
from drifthunter.prices.sharadar import SharadarProvider
from drifthunter.prices.free import FreeProvider
from drifthunter.scorer.models import Signal
from drifthunter.backtest.event_study import (
    LOOKBACK_CAL_DAYS,
    LOOKAHEAD_CAL_DAYS,
    run_event_study,
)
from drifthunter.backtest.stats import bootstrap_ci, BootstrapCI


SEED = 42
N_ITER = 10_000


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def load_signals(cfg) -> list[Signal]:
    """Reconstruct Signal objects from data/signals.parquet, preserving the
    same row order used by `drifthunter study` (and therefore the same
    by-ticker grouping / range computation in run_event_study)."""
    df = pd.read_parquet(cfg.data_dir / "signals.parquet")
    return [
        Signal(
            profile=row.profile,
            ticker=row.ticker,
            trigger_date=row.trigger_date.date(),
            score=row.score,
            detail=row.detail,
        )
        for row in df.itertuples()
    ]


def per_ticker_ranges(signals: list[Signal], max_horizon: int) -> dict[str, tuple[date, date]]:
    """Mirror run_event_study's per-ticker date-range computation exactly so
    CachingProvider keys match the cache written by `drifthunter study`."""
    by_ticker: dict[str, list[date]] = {}
    for sig in signals:
        by_ticker.setdefault(sig.ticker, []).append(sig.trigger_date)

    ranges: dict[str, tuple[date, date]] = {}
    for ticker, triggers in by_ticker.items():
        start = min(triggers) - timedelta(days=LOOKBACK_CAL_DAYS)
        end = max(triggers) + timedelta(days=LOOKAHEAD_CAL_DAYS + 2 * max_horizon)
        ranges[ticker] = (start, end)
    return ranges


def fetch_ticker_frames(provider, ranges: dict[str, tuple[date, date]]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for ticker, (start, end) in ranges.items():
        frames[ticker] = provider.daily(ticker, start, end)
    return frames


def global_span(ranges: dict[str, tuple[date, date]]) -> tuple[date, date]:
    starts = [s for s, _ in ranges.values()]
    ends = [e for _, e in ranges.values()]
    return min(starts), max(ends)


# ---------------------------------------------------------------------------
# Diagnostic A: the missed day-0 pop
# ---------------------------------------------------------------------------

def diagnostic_a(signals: list[Signal], ticker_frames: dict[str, pd.DataFrame],
                  spy: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], dict]:
    """Per-signal day0 / missed_total returns (raw and SPY-adjusted).

    Returns (per_profile_dataframes, skip_counts).
    """
    rows: list[dict] = []
    skip_counts = {"no_prices": 0, "no_entry": 0, "no_pre": 0}

    for sig in signals:
        px = ticker_frames.get(sig.ticker)
        if px is None or px.empty:
            skip_counts["no_prices"] += 1
            continue

        trigger_ts = pd.Timestamp(sig.trigger_date)

        future = px[px.index > trigger_ts]
        if future.empty:
            skip_counts["no_entry"] += 1
            continue
        entry_date = future.index[0]
        entry_pos = px.index.get_loc(entry_date)

        past = px[px.index < trigger_ts]
        if past.empty:
            skip_counts["no_pre"] += 1
            continue
        pre_date = past.index[-1]

        pre_close = float(px.loc[pre_date, "close"])
        entry_open = float(px.iloc[entry_pos]["open"])

        missed_total = entry_open / pre_close - 1.0

        day0_return = float("nan")
        has_day0 = trigger_ts in px.index
        if has_day0:
            trigger_close = float(px.loc[trigger_ts, "close"])
            day0_return = trigger_close / pre_close - 1.0

        # SPY-adjusted versions: subtract SPY's return over the same
        # date pairs, when both endpoints exist in the SPY frame.
        missed_total_excess = float("nan")
        if pre_date in spy.index and entry_date in spy.index:
            spy_pre_close = float(spy.loc[pre_date, "close"])
            spy_entry_open = float(spy.loc[entry_date, "open"])
            spy_missed = spy_entry_open / spy_pre_close - 1.0
            missed_total_excess = missed_total - spy_missed

        day0_return_excess = float("nan")
        if has_day0 and pre_date in spy.index and trigger_ts in spy.index:
            spy_pre_close = float(spy.loc[pre_date, "close"])
            spy_trigger_close = float(spy.loc[trigger_ts, "close"])
            spy_day0 = spy_trigger_close / spy_pre_close - 1.0
            day0_return_excess = day0_return - spy_day0

        rows.append({
            "profile": sig.profile,
            "ticker": sig.ticker,
            "trigger_date": sig.trigger_date,
            "has_day0": has_day0,
            "day0_return": day0_return,
            "missed_total": missed_total,
            "day0_return_excess": day0_return_excess,
            "missed_total_excess": missed_total_excess,
        })

    df = pd.DataFrame(rows)
    per_profile = {p: g for p, g in df.groupby("profile")} if not df.empty else {}
    return per_profile, skip_counts


def _ci_or_none(values: pd.Series) -> BootstrapCI | None:
    arr = values.to_numpy(dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        return None
    return bootstrap_ci(arr, n_iter=N_ITER, seed=SEED)


def summarize_diagnostic_a(per_profile: dict[str, pd.DataFrame]) -> dict[str, dict]:
    metrics = ["day0_return", "missed_total", "day0_return_excess", "missed_total_excess"]
    out: dict[str, dict] = {}
    for profile, g in per_profile.items():
        out[profile] = {}
        for m in metrics:
            ci = _ci_or_none(g[m])
            out[profile][m] = ci
    return out


# ---------------------------------------------------------------------------
# Diagnostic B: IWM benchmark decomposition
# ---------------------------------------------------------------------------

def diagnostic_b(signals: list[Signal], iwm_provider, cfg) -> pd.DataFrame:
    return run_event_study(
        signals, iwm_provider, cfg.tradability,
        horizons=cfg.study.horizons,
        cost_bps_list=cfg.study.cost_bps_sweep,
        benchmark="IWM",
    )


def summarize_excess(events: pd.DataFrame, profile: str, horizon: int,
                      headline_cost_bps: int) -> BootstrapCI | None:
    sl = events[
        (events["profile"] == profile)
        & (events["horizon"] == horizon)
        & (events["cost_bps"] == headline_cost_bps)
        & (events["filter_reason"] == "")
    ]
    if sl.empty:
        return None
    return bootstrap_ci(sl["excess_return"].to_numpy(dtype=float), n_iter=N_ITER, seed=SEED)


# ---------------------------------------------------------------------------
# Sanity check
# ---------------------------------------------------------------------------

def sanity_check(events_path: Path, ticker_frames: dict[str, pd.DataFrame]) -> str:
    """Recompute entry_date/entry_open for one completed event from the
    Diagnostic-A price frames and compare against data/events.parquet."""
    events = pd.read_parquet(events_path)
    completed = events[(events["cost_bps"] == 30) & (events["filter_reason"] == "")]
    if completed.empty:
        return "SANITY CHECK: no completed events found in data/events.parquet"

    row = completed.iloc[0]
    ticker = row["ticker"]
    trigger_ts = pd.Timestamp(row["trigger_date"])
    expected_entry = pd.Timestamp(row["entry_date"])

    px = ticker_frames.get(ticker)
    if px is None or px.empty:
        return f"SANITY CHECK: no price frame for {ticker} (cache miss?)"

    future = px[px.index > trigger_ts]
    if future.empty:
        return f"SANITY CHECK: no rows after trigger for {ticker}"

    recomputed_entry = future.index[0]
    recomputed_open = float(px.loc[recomputed_entry, "open"])

    # raw_return for horizon recorded in the row, recomputed from this frame
    horizon = int(row["horizon"])
    entry_pos = px.index.get_loc(recomputed_entry)
    exit_pos = entry_pos + horizon
    recomputed_raw = float("nan")
    if exit_pos < len(px):
        exit_open = float(px.iloc[exit_pos]["open"])
        recomputed_raw = exit_open / recomputed_open - 1.0

    ok_entry_date = recomputed_entry == expected_entry
    ok_raw = (
        not np.isnan(recomputed_raw)
        and abs(recomputed_raw - float(row["raw_return"])) < 1e-9
    )

    lines = [
        "SANITY CHECK (Diagnostic-A price logic vs data/events.parquet):",
        f"  ticker={ticker} trigger_date={trigger_ts.date()} horizon={horizon}",
        f"  events.parquet entry_date={expected_entry.date()} raw_return={row['raw_return']:.6f}",
        f"  recomputed       entry_date={recomputed_entry.date()} entry_open={recomputed_open:.4f} "
        f"raw_return={recomputed_raw:.6f}",
        f"  entry_date match: {ok_entry_date}; raw_return match: {ok_raw}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _fmt_ci(ci: BootstrapCI | None) -> str:
    if ci is None:
        return "n/a"
    return f"n={ci.n}, mean={ci.mean:+.4f}, 95% CI=[{ci.lo:+.4f}, {ci.hi:+.4f}]"


def render_diagnostic_a_table(profile: str, summary: dict[str, BootstrapCI | None]) -> list[str]:
    lines = [
        f"### Profile: {profile}",
        "",
        "| Metric | N | Mean | 95% CI lo | 95% CI hi |",
        "|---|---|---|---|---|",
    ]
    labels = {
        "day0_return": "day0_return (raw)",
        "day0_return_excess": "day0_return (SPY-adjusted)",
        "missed_total": "missed_total (raw)",
        "missed_total_excess": "missed_total (SPY-adjusted)",
    }
    for key, label in labels.items():
        ci = summary.get(key)
        if ci is None:
            lines.append(f"| {label} | - | - | - | - |")
        else:
            lines.append(f"| {label} | {ci.n} | {ci.mean:+.4f} | {ci.lo:+.4f} | {ci.hi:+.4f} |")
    lines.append("")
    return lines


def render_diagnostic_b_table(profile: str, horizons: list[int], headline_cost_bps: int,
                               events_iwm: pd.DataFrame, events_spy: pd.DataFrame) -> list[str]:
    lines = [
        f"### Profile: {profile} (cost_bps={headline_cost_bps})",
        "",
        "| Horizon | N (SPY) | Mean excess vs SPY | 95% CI vs SPY | N (IWM) | Mean excess vs IWM | 95% CI vs IWM |",
        "|---|---|---|---|---|---|---|",
    ]
    for h in horizons:
        ci_spy = summarize_excess(events_spy, profile, h, headline_cost_bps)
        ci_iwm = summarize_excess(events_iwm, profile, h, headline_cost_bps)

        n_spy = ci_spy.n if ci_spy else "-"
        mean_spy = f"{ci_spy.mean:+.4f}" if ci_spy else "-"
        ci_spy_s = f"[{ci_spy.lo:+.4f}, {ci_spy.hi:+.4f}]" if ci_spy else "-"

        n_iwm = ci_iwm.n if ci_iwm else "-"
        mean_iwm = f"{ci_iwm.mean:+.4f}" if ci_iwm else "-"
        ci_iwm_s = f"[{ci_iwm.lo:+.4f}, {ci_iwm.hi:+.4f}]" if ci_iwm else "-"

        lines.append(
            f"| {h} | {n_spy} | {mean_spy} | {ci_spy_s} | {n_iwm} | {mean_iwm} | {ci_iwm_s} |"
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Interpretation helpers
# ---------------------------------------------------------------------------

def interpret_a(profile: str, summary: dict[str, BootstrapCI | None]) -> str:
    missed_excess = summary.get("missed_total_excess")
    day0_excess = summary.get("day0_return_excess")

    if missed_excess is None:
        return (
            f"{profile}: insufficient data to assess the missed day-0 pop "
            f"(no SPY-aligned date pairs)."
        )

    missed_sig = missed_excess.lo > 0
    missed_zero = missed_excess.lo <= 0 <= missed_excess.hi

    if missed_sig:
        verdict = (
            f"missed_total excess is significantly POSITIVE (mean "
            f"{missed_excess.mean:+.4f}, 95% CI [{missed_excess.lo:+.4f}, "
            f"{missed_excess.hi:+.4f}]), consistent with the documented "
            f"filing reaction concentrating BEFORE our entry -- a timing "
            f"problem rather than a dead signal."
        )
    elif missed_zero:
        verdict = (
            f"missed_total excess is statistically indistinguishable from "
            f"zero (mean {missed_excess.mean:+.4f}, 95% CI "
            f"[{missed_excess.lo:+.4f}, {missed_excess.hi:+.4f}]), "
            f"consistent with no detectable reaction at daily granularity "
            f"around the filing date -- i.e. the signal does not appear to "
            f"carry a pre-entry drift to miss."
        )
    else:
        verdict = (
            f"missed_total excess is significantly NEGATIVE (mean "
            f"{missed_excess.mean:+.4f}, 95% CI [{missed_excess.lo:+.4f}, "
            f"{missed_excess.hi:+.4f}]) -- prices drift down, on average, "
            f"between the last pre-filing close and our entry."
        )

    if day0_excess is not None:
        verdict += (
            f" On the subset of signals where trigger_date itself was a "
            f"trading day, the same-day excess move (day0_return_excess) "
            f"has mean {day0_excess.mean:+.4f}, 95% CI "
            f"[{day0_excess.lo:+.4f}, {day0_excess.hi:+.4f}]."
        )

    return f"**{profile}**: {verdict}"


def interpret_b(profile: str, horizons: list[int], events_iwm: pd.DataFrame,
                 events_spy: pd.DataFrame, headline_cost_bps: int) -> str:
    diffs = []
    spy_means = []
    iwm_means = []
    for h in horizons:
        ci_spy = summarize_excess(events_spy, profile, h, headline_cost_bps)
        ci_iwm = summarize_excess(events_iwm, profile, h, headline_cost_bps)
        if ci_spy is None or ci_iwm is None:
            continue
        spy_means.append(ci_spy.mean)
        iwm_means.append(ci_iwm.mean)
        diffs.append(ci_iwm.mean - ci_spy.mean)

    if not diffs:
        return f"**{profile}**: insufficient data to compare SPY vs IWM excess returns."

    avg_diff = sum(diffs) / len(diffs)
    avg_spy = sum(spy_means) / len(spy_means)
    avg_iwm = sum(iwm_means) / len(iwm_means)

    both_negative_or_flat = avg_iwm <= 0.0
    spy_more_negative = avg_spy < avg_iwm

    if both_negative_or_flat:
        verdict = (
            f"excess returns vs IWM remain non-positive on average "
            f"(mean across horizons {avg_iwm:+.4f} vs {avg_spy:+.4f} vs SPY), "
            f"so switching the benchmark from SPY to a small/mid-cap index "
            f"does not turn the result positive -- the underlying signal "
            f"itself does not appear to beat its peer-universe drift "
            f"('signal dead' rather than purely a 'regime headwind' "
            f"explanation)."
        )
    elif spy_more_negative:
        verdict = (
            f"excess returns vs IWM are higher than vs SPY on average "
            f"(mean across horizons {avg_iwm:+.4f} vs {avg_spy:+.4f} vs SPY, "
            f"diff {avg_diff:+.4f}), consistent with part of the SPY-relative "
            f"underperformance reflecting a small/mid-cap regime headwind "
            f"during the study window rather than purely a dead signal -- "
            f"though the IWM-relative numbers should be checked against the "
            f"table above for whether they are themselves significantly "
            f"positive."
        )
    else:
        verdict = (
            f"excess returns vs IWM are similar to or lower than vs SPY "
            f"(mean across horizons {avg_iwm:+.4f} vs {avg_spy:+.4f} vs SPY, "
            f"diff {avg_diff:+.4f}); the small/mid-cap benchmark does not "
            f"materially change the picture."
        )

    return f"**{profile}**: {verdict}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config(REPO_ROOT / "config.yaml")

    print("=" * 78)
    print("DriftHunter Phase 0 post-mortem diagnostics")
    print("Post-mortem diagnostic only -- the Phase 0 verdict (KILL vs SPY per")
    print("spec Sec3.6) is unaffected.")
    print("=" * 78)

    signals = load_signals(cfg)
    print(f"\nLoaded {len(signals)} signals "
          f"({sum(1 for s in signals if s.profile == 'form4')} form4, "
          f"{sum(1 for s in signals if s.profile == 'sc13d')} sc13d)")

    max_horizon = max(cfg.study.horizons)
    ranges = per_ticker_ranges(signals, max_horizon)
    g_start, g_end = global_span(ranges)
    print(f"Global span: {g_start} -> {g_end} (max_horizon={max_horizon})")

    # --- Provider stack: SPY-routed (matches `drifthunter study` exactly) ---
    spy_provider = _make_provider(cfg)

    print(f"\nFetching {len(ranges)} ticker frames via cache (SPY-routed provider)...")
    ticker_frames = fetch_ticker_frames(spy_provider, ranges)
    n_empty = sum(1 for f in ticker_frames.values() if f.empty)
    print(f"  {len(ticker_frames) - n_empty}/{len(ticker_frames)} tickers have price data "
          f"({n_empty} empty)")

    print(f"\nFetching SPY frame for {g_start} -> {g_end}...")
    spy = spy_provider.daily(cfg.study.benchmark, g_start, g_end)
    print(f"  SPY frame: {len(spy)} rows, {spy.index.min()} -> {spy.index.max()}" if not spy.empty
          else "  SPY frame EMPTY")

    # --- Sanity check (Diagnostic-A price logic vs events.parquet) ---
    events_path = cfg.data_dir / "events.parquet"
    sanity = sanity_check(events_path, ticker_frames)
    print("\n" + sanity)

    # --- Diagnostic A ---
    print("\n" + "-" * 78)
    print("Diagnostic A: missed day-0 pop")
    print("-" * 78)
    per_profile_a, skip_counts = diagnostic_a(signals, ticker_frames, spy)
    print(f"Skipped: {skip_counts}")

    summary_a: dict[str, dict[str, BootstrapCI | None]] = {}
    for profile, g in per_profile_a.items():
        summary_a[profile] = summarize_diagnostic_a({profile: g})[profile]
        print(f"\n{profile} (n={len(g)}):")
        for key, ci in summary_a[profile].items():
            print(f"  {key:28s} {_fmt_ci(ci)}")

    # --- Diagnostic B ---
    print("\n" + "-" * 78)
    print("Diagnostic B: IWM benchmark decomposition")
    print("-" * 78)

    iwm_provider = CachingProvider(
        BenchmarkRoutingProvider(SharadarProvider(os.environ["NASDAQ_DATA_LINK_API_KEY"]), "IWM"),
        cfg.data_dir / "prices",
    )

    iwm_fetch_ok = True
    try:
        iwm_frame_probe = iwm_provider.daily("IWM", g_start, g_end)
        if iwm_frame_probe.empty:
            iwm_fetch_ok = False
            print("WARNING: IWM benchmark frame is empty (fetch failed via yfinance/Stooq).")
    except Exception as exc:  # pragma: no cover - network/runtime fallback path
        iwm_fetch_ok = False
        print(f"WARNING: IWM benchmark fetch raised {exc!r}.")

    events_iwm = pd.DataFrame()
    if iwm_fetch_ok:
        print("Running event study with benchmark=IWM (this may take a few minutes)...")
        events_iwm = diagnostic_b(signals, iwm_provider, cfg)
        print(f"  produced {len(events_iwm)} rows")
    else:
        print("Skipping Diagnostic B event study run (IWM data unavailable); "
              "report will note partial results.")

    events_spy = pd.read_parquet(events_path)

    headline = cfg.study.headline_cost_bps
    profiles = sorted(set(s.profile for s in signals))

    for profile in profiles:
        print(f"\n{profile} (cost_bps={headline}):")
        for h in cfg.study.horizons:
            ci_spy = summarize_excess(events_spy, profile, h, headline)
            ci_iwm = summarize_excess(events_iwm, profile, h, headline) if iwm_fetch_ok else None
            print(f"  h={h:>3}  SPY: {_fmt_ci(ci_spy)}")
            print(f"          IWM: {_fmt_ci(ci_iwm)}")

    # --- Render markdown ---
    lines: list[str] = []
    lines.append("# DriftHunter Phase 0 Post-Mortem Diagnostics")
    lines.append("")
    lines.append(
        "**Purpose**: explain WHY the Phase 0 gate returned KILL for both "
        "profiles (see `data/report.md`). This document does not change any "
        "engine code, gate logic, or the existing report."
    )
    lines.append("")
    lines.append(
        "> **Post-mortem diagnostic only -- the Phase 0 verdict (KILL vs SPY "
        "per spec Sec3.6) is unaffected.**"
    )
    lines.append("")
    lines.append(f"Signals analyzed: {len(signals)} "
                  f"({sum(1 for s in signals if s.profile == 'form4')} form4, "
                  f"{sum(1 for s in signals if s.profile == 'sc13d')} sc13d). "
                  f"Global price span: {g_start} -> {g_end}.")
    lines.append("")
    lines.append("```")
    lines.append(sanity)
    lines.append("```")
    lines.append("")

    # Diagnostic A
    lines.append("## Diagnostic A: the missed day-0 pop (form4 + sc13d separately)")
    lines.append("")
    lines.append(
        "Question: did the documented filing reaction happen BEFORE our "
        "entry (next open after `trigger_date`)?"
    )
    lines.append("")
    lines.append(
        "- `day0_return`: for signals where `trigger_date` itself was a "
        "trading day, `close[trigger] / close[pre] - 1` -- the filing-day "
        "move we could never capture."
    )
    lines.append(
        "- `missed_total`: `open[entry] / close[pre] - 1` -- everything "
        "between the last pre-filing close and our actual entry price (the "
        "full \"pop we missed\")."
    )
    lines.append(
        "- `*_excess` variants subtract SPY's return over the identical "
        "date pairs."
    )
    lines.append(
        f"- Skipped signals: {skip_counts['no_prices']} (no price data), "
        f"{skip_counts['no_entry']} (no row after trigger_date), "
        f"{skip_counts['no_pre']} (no row before trigger_date)."
    )
    lines.append("")
    for profile in profiles:
        if profile not in summary_a:
            lines.append(f"### Profile: {profile}")
            lines.append("")
            lines.append("No data (all signals skipped).")
            lines.append("")
            continue
        lines += render_diagnostic_a_table(profile, summary_a[profile])

    lines.append("### Interpretation")
    lines.append("")
    lines.append(
        "Rule: if `missed_total` excess is significantly POSITIVE while the "
        "event study's post-entry excess was ~0/negative, the reaction "
        "concentrates before entry (timing problem); if `missed_total` is "
        "also ~0, the signal simply has no reaction at daily granularity "
        "(dead signal)."
    )
    lines.append("")
    for profile in profiles:
        if profile in summary_a:
            lines.append(interpret_a(profile, summary_a[profile]))
            lines.append("")

    # Diagnostic B
    lines.append("## Diagnostic B: IWM benchmark decomposition")
    lines.append("")
    lines.append(
        "The Phase 0 event study re-run with `benchmark=\"IWM\"` (Russell "
        "2000 ETF, a small/mid-cap proxy) instead of `benchmark=\"SPY\"`, "
        "compared side-by-side with the SPY numbers already recorded in "
        "`data/events.parquet` (same slice: `cost_bps == 30`, "
        "`filter_reason == \"\"`)."
    )
    lines.append("")
    if not iwm_fetch_ok:
        lines.append(
            "> **PARTIAL RESULTS**: the IWM benchmark frame could not be "
            "fetched (yfinance and Stooq both failed). Only the SPY columns "
            "below are populated; IWM columns are marked `-`."
        )
        lines.append("")

    for profile in profiles:
        lines += render_diagnostic_b_table(
            profile, cfg.study.horizons, headline, events_iwm, events_spy
        )

    lines.append("### Interpretation")
    lines.append("")
    if iwm_fetch_ok:
        for profile in profiles:
            lines.append(interpret_b(profile, cfg.study.horizons, events_iwm, events_spy, headline))
            lines.append("")
    else:
        lines.append(
            "IWM comparison unavailable (fetch failed); cannot distinguish "
            "\"signal dead\" from \"regime headwind\" with this run."
        )
        lines.append("")

    # What this means for future work
    lines.append("## What this means for future work")
    lines.append("")
    future_points: list[str] = []

    for profile in profiles:
        s = summary_a.get(profile)
        if s is None:
            continue
        missed_excess = s.get("missed_total_excess")
        if missed_excess is not None and missed_excess.lo > 0:
            future_points.append(
                f"- **{profile}**: `missed_total` excess CI is entirely "
                f"positive ([{missed_excess.lo:+.4f}, {missed_excess.hi:+.4f}], "
                f"n={missed_excess.n}). The data supports that, on average, "
                f"price moves up between the last pre-filing close and our "
                f"entry -- any future redesign aiming to capture this would "
                f"need a same-day-of-filing entry mechanism (not available "
                f"with daily-bar data and a next-open entry rule), or "
                f"intraday data."
            )
        elif missed_excess is not None and missed_excess.lo <= 0 <= missed_excess.hi:
            future_points.append(
                f"- **{profile}**: `missed_total` excess CI straddles zero "
                f"([{missed_excess.lo:+.4f}, {missed_excess.hi:+.4f}], "
                f"n={missed_excess.n}). The data does not support a "
                f"pre-entry drift large enough to explain the post-entry "
                f"underperformance; at daily granularity there is no "
                f"detectable reaction to recapture."
            )
        elif missed_excess is not None:
            future_points.append(
                f"- **{profile}**: `missed_total` excess CI is entirely "
                f"negative ([{missed_excess.lo:+.4f}, {missed_excess.hi:+.4f}], "
                f"n={missed_excess.n}). The data does not support a missed "
                f"upside pop; if anything, prices already drift down before "
                f"our entry."
            )

    if iwm_fetch_ok:
        for profile in profiles:
            diffs = []
            for h in cfg.study.horizons:
                ci_spy = summarize_excess(events_spy, profile, h, headline)
                ci_iwm = summarize_excess(events_iwm, profile, h, headline)
                if ci_spy and ci_iwm:
                    diffs.append(ci_iwm.mean - ci_spy.mean)
            if diffs:
                avg_diff = sum(diffs) / len(diffs)
                avg_iwm = sum(
                    summarize_excess(events_iwm, profile, h, headline).mean
                    for h in cfg.study.horizons
                    if summarize_excess(events_iwm, profile, h, headline)
                ) / len(diffs)
                if avg_iwm <= 0:
                    future_points.append(
                        f"- **{profile}**: average IWM-relative excess across "
                        f"horizons is {avg_iwm:+.4f} (still <= 0), so "
                        f"benchmark choice (SPY vs a small/mid-cap index) "
                        f"does not by itself explain the KILL verdict -- the "
                        f"data does not support a 'pure regime headwind' "
                        f"story for this profile."
                    )
                else:
                    future_points.append(
                        f"- **{profile}**: average IWM-relative excess "
                        f"({avg_iwm:+.4f}) is higher than the SPY-relative "
                        f"excess (avg diff {avg_diff:+.4f}), so part of the "
                        f"SPY-relative underperformance may reflect "
                        f"small/mid-cap regime exposure during the study "
                        f"window rather than the signal itself; this is "
                        f"informational only and does not change the "
                        f"pre-committed KILL verdict (which is defined "
                        f"relative to SPY)."
                    )

    if not future_points:
        future_points.append(
            "- No diagnostic produced a clear, statistically supported "
            "direction; results above should be read at face value without "
            "further inference."
        )

    lines.extend(future_points)
    lines.append("")

    out_path = cfg.data_dir / "diagnostics.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
