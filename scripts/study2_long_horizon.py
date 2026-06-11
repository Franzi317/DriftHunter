"""Study 2 -- Long-horizon information content of insider clusters & 13Ds.

Spec: `docs/study2-long-horizon-insider.md` (commit 0faa4ac, criteria frozen
BEFORE this script computed any return statistic). This script does NOT
modify engine code, the Phase 0 gate, or that spec. It is a standalone
evaluation re-using `run_event_study` exactly as `drifthunter study` does,
at horizons [60, 125, 250] / cost 30bp / benchmark SPY (primary) and IWM
(labeled secondary diagnostic only -- per spec it cannot flip a verdict).

Verdict labels are SIGNAL / NO-SIGNAL (a knowledge question), not GO/KILL.

Outputs:
    data/study2_events_spy.parquet  (intermediate; gitignored)
    data/study2_events_iwm.parquet  (intermediate; gitignored)
    data/study2-report.md

Usage:
    uv run python scripts/study2_long_horizon.py

Runtime note: price windows extend ~590 calendar days past each signal's
trigger_date (LOOKAHEAD_CAL_DAYS=90 + 2*max_horizon=250 -> 590) to cover the
250-day exit. These windows differ from the Phase 0 cache keys (which only
need to cover horizon<=40), so this is effectively a COLD CACHE for most
tickers: expect a long first run (price fetches for ~thousands of tickers).
Re-runs against a warm cache are fast and fully deterministic.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Sharadar key comes from the environment. Cached price reads never touch
# the network, so a re-run against a warm cache works without any key.
if not os.environ.get("NASDAQ_DATA_LINK_API_KEY"):
    print("NOTE: NASDAQ_DATA_LINK_API_KEY not set; relying on the local "
          "price cache only.", file=sys.stderr)

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from drifthunter.config import Config, load_config
from drifthunter.prices.provider import CachingProvider
from drifthunter.prices.sharadar import SharadarProvider
from drifthunter.prices.free import FreeProvider
from drifthunter.scorer.models import Signal
from drifthunter.backtest.event_study import run_event_study
from drifthunter.backtest.stats import bootstrap_ci, yearly_means


# ---------------------------------------------------------------------------
# Frozen criteria (spec section "Pre-committed SIGNAL criteria")
# ---------------------------------------------------------------------------

HORIZONS = [60, 125, 250]
COST_BPS = 30
HEADLINE_COST_BPS = 30
SEED = 42
N_ITER = 10_000
ALPHA = 0.01  # 99% CI

MIN_N = {"form4": 300, "sc13d": 150}
ECONOMIC_FLOOR = {60: 0.01, 125: 0.02, 250: 0.03}  # decimals (1.0% / 2.0% / 3.0%)
MIN_POSITIVE_YEARS = 3
MIN_COVERAGE = 0.80


# ---------------------------------------------------------------------------
# Provider stack
# ---------------------------------------------------------------------------

class SpyIwmFreeRoutingProvider:
    """Routes both "SPY" and "IWM" to the free provider (Sharadar SEP covers
    equities, not ETFs; the benchmarks have no survivorship concern), all
    other tickers to the configured primary provider.

    Local to this script (not a cli.py change): the SPY pass needs only SPY
    routed free, but the IWM diagnostic pass additionally needs IWM routed
    free, and both passes share the same per-ticker cache, so a single
    provider that routes both benchmark tickers free keeps cache keys
    consistent across the two `run_event_study` calls.
    """

    def __init__(self, primary, free_tickers: tuple[str, ...] = ("SPY", "IWM")):
        self._primary = primary
        self._free = FreeProvider()
        self._free_tickers = set(free_tickers)

    def daily(self, ticker, start, end):
        if ticker in self._free_tickers:
            return self._free.daily(ticker, start, end)
        return self._primary.daily(ticker, start, end)


def make_provider(cfg: Config) -> CachingProvider:
    api_key = os.environ.get(cfg.prices.nasdaq_api_key_env, "")
    inner = SpyIwmFreeRoutingProvider(SharadarProvider(api_key))
    return CachingProvider(inner, cfg.data_dir / "prices")


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def load_signals(cfg: Config) -> list[Signal]:
    """Reconstruct Signal objects from data/signals.parquet, identical to the
    `drifthunter study` command (same row order -> same per-ticker grouping
    and date-range computation in run_event_study)."""
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


# ---------------------------------------------------------------------------
# Evaluation (pure function -- no I/O)
# ---------------------------------------------------------------------------

@dataclass
class HorizonResult:
    horizon: int
    n: int
    mean: float
    ci_lo: float
    ci_hi: float
    floor: float
    positive_years: int
    total_years: int
    iwm_n: int | None
    iwm_mean: float | None
    iwm_ci_lo: float | None
    iwm_ci_hi: float | None
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    @property
    def iwm_negative_flag(self) -> bool:
        """True if this horizon PASSED all criteria but its IWM-relative
        mean is negative (spec: must be flagged prominently -- size-regime
        exposure rather than stock selection)."""
        return self.passed and self.iwm_mean is not None and self.iwm_mean < 0


@dataclass
class ProfileVerdict:
    profile: str
    decision: str  # "SIGNAL" | "NO-SIGNAL", optionally "SUSPECT-" prefixed
    coverage_rate: float
    passing_horizons: list[int]
    horizon_results: list[HorizonResult] = field(default_factory=list)


def _completed(events: pd.DataFrame, profile: str, horizon: int,
               cost_bps: int = COST_BPS) -> pd.DataFrame:
    if events.empty:
        return events
    return events[
        (events["profile"] == profile)
        & (events["horizon"] == horizon)
        & (events["cost_bps"] == cost_bps)
        & (events["filter_reason"] == "")
    ]


def evaluate_study2(events_spy: pd.DataFrame, events_iwm: pd.DataFrame,
                     coverage: dict, *,
                     horizons: list[int] = HORIZONS,
                     n_iter: int = N_ITER, seed: int = SEED, alpha: float = ALPHA,
                     ) -> list[ProfileVerdict]:
    """Apply the five pre-committed Study 2 criteria to event-study output.

    Pure function: no file I/O, no provider calls. `events_spy`/`events_iwm`
    are `run_event_study` output frames (any cost_bps sweep is fine; only
    `COST_BPS=30` rows are used). `coverage` is the parsed
    `data/coverage.json` dict (profile -> {"rate": float, ...}).

    Criteria (ALL must hold at >=1 horizon for SIGNAL; spec section
    "Pre-committed SIGNAL criteria"):
      1. n >= 300 (form4) / n >= 150 (sc13d) completed events at that horizon.
      2. 99% bootstrap CI lower bound > 0 on mean per-event excess return
         (alpha=0.01, seed=42, n_iter=10_000).
      3. Economic floor on the mean excess: >=1.0% (60d), >=2.0% (125d),
         >=3.0% (250d).
      4. Positive mean excess in >=3 of 5 signal-vintage calendar years.
      5. Price coverage >= 80% for the profile (else SUSPECT-* prefix on the
         decision; coverage never changes SIGNAL vs NO-SIGNAL itself).
    """
    verdicts: list[ProfileVerdict] = []
    profiles = sorted(set(events_spy["profile"]).union(coverage.keys()))

    for profile in profiles:
        min_n = MIN_N.get(profile)
        cov_rate = float(coverage.get(profile, {}).get("rate", 0.0))

        horizon_results: list[HorizonResult] = []
        passing_horizons: list[int] = []

        for h in horizons:
            spy_df = _completed(events_spy, profile, h)
            n = len(spy_df)

            if n == 0:
                # No completed events at this horizon: every criterion that
                # depends on data fails; record a degenerate row.
                horizon_results.append(HorizonResult(
                    horizon=h, n=0, mean=float("nan"), ci_lo=float("nan"),
                    ci_hi=float("nan"), floor=ECONOMIC_FLOOR[h],
                    positive_years=0, total_years=0,
                    iwm_n=None, iwm_mean=None, iwm_ci_lo=None, iwm_ci_hi=None,
                    checks={
                        f"n>={min_n}": False,
                        "CI99 lo > 0": False,
                        f"mean>={ECONOMIC_FLOOR[h]:+.1%}": False,
                        f">={MIN_POSITIVE_YEARS} positive vintage years": False,
                    },
                ))
                continue

            ci = bootstrap_ci(spy_df["excess_return"].to_numpy(dtype=float),
                               n_iter=n_iter, seed=seed, alpha=alpha)

            years = yearly_means(spy_df)
            positive_years = sum(1 for v in years.values() if v > 0)

            # IWM diagnostic (labeled secondary; never affects checks)
            iwm_df = _completed(events_iwm, profile, h)
            iwm_n: int | None = None
            iwm_mean: float | None = None
            iwm_ci_lo: float | None = None
            iwm_ci_hi: float | None = None
            if not iwm_df.empty:
                iwm_ci = bootstrap_ci(iwm_df["excess_return"].to_numpy(dtype=float),
                                       n_iter=n_iter, seed=seed, alpha=alpha)
                iwm_n, iwm_mean, iwm_ci_lo, iwm_ci_hi = (
                    iwm_ci.n, iwm_ci.mean, iwm_ci.lo, iwm_ci.hi
                )

            floor = ECONOMIC_FLOOR[h]
            checks = {
                f"n>={min_n}": n >= (min_n or 0),
                "CI99 lo > 0": ci.lo > 0,
                f"mean>={floor:+.1%}": ci.mean >= floor,
                f">={MIN_POSITIVE_YEARS} positive vintage years": positive_years >= MIN_POSITIVE_YEARS,
            }

            horizon_results.append(HorizonResult(
                horizon=h, n=n, mean=ci.mean, ci_lo=ci.lo, ci_hi=ci.hi,
                floor=floor, positive_years=positive_years, total_years=len(years),
                iwm_n=iwm_n, iwm_mean=iwm_mean, iwm_ci_lo=iwm_ci_lo, iwm_ci_hi=iwm_ci_hi,
                checks=checks,
            ))

            if all(checks.values()):
                passing_horizons.append(h)

        decision = "SIGNAL" if passing_horizons else "NO-SIGNAL"
        if cov_rate < MIN_COVERAGE:
            decision = f"SUSPECT-{decision}"

        verdicts.append(ProfileVerdict(
            profile=profile, decision=decision, coverage_rate=cov_rate,
            passing_horizons=passing_horizons, horizon_results=horizon_results,
        ))

    return verdicts


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _fmt_pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "n/a"
    return f"{x:+.2%}"


def _fmt_ci_pct(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None or pd.isna(lo) or pd.isna(hi):
        return "n/a"
    return f"[{lo:+.2%}, {hi:+.2%}]"


SPEC_SUMMARY = """\
This report was generated by `scripts/study2_long_horizon.py` against the
frozen criteria in `docs/study2-long-horizon-insider.md` (commit 0faa4ac,
criteria committed BEFORE any Study 2 return statistic was computed). The
five SIGNAL criteria below are restated verbatim from that spec; this script
does not alter them.

Pre-committed SIGNAL criteria (per profile; ALL must hold at >=1 horizon):

1. n >= 300 (form4) / n >= 150 (sc13d) completed events at that horizon.
2. 99% bootstrap CI lower bound > 0 on mean per-event excess return after
   30bp costs vs SPY (alpha=0.01; seed 42; 10,000 iterations).
3. Economic significance floor on the mean excess at that horizon:
   >= +1.0% (60d), >= +2.0% (125d), >= +3.0% (250d).
4. Positive mean excess in >= 3 of 5 signal-vintage calendar years.
5. Price coverage >= 80% for the profile (else verdict is SUSPECT-*).

Anything failing all horizons on any criterion -> NO-SIGNAL. Descriptive
portfolio statistics may be reported but are explicitly NOT verdict inputs.
IWM (Russell 2000 ETF) figures are a labeled secondary diagnostic only and
cannot flip a verdict; per spec, any SIGNAL accompanied by a NEGATIVE
IWM-relative mean at the same horizon is flagged prominently below.
"""

INTERPRETATIONS = {
    "both_no_signal": (
        "**Pre-registered interpretation (NO-SIGNAL both profiles)**: the "
        "book closes on filing-following entirely, including as a manual "
        "idea source. This was the expected base case."
    ),
    "form4_signal": (
        "**Pre-registered interpretation (SIGNAL on form4 only)**: "
        "consistent with the literature; would justify (at most) a new "
        "deployment-design phase for slow, low-turnover position following "
        "-- with fresh gates."
    ),
    "sc13d_signal": (
        "**Pre-registered interpretation (SIGNAL on sc13d only)**: activist "
        "campaigns pay out long; same caveat (a new deployment-design phase "
        "with fresh gates would be required, not an immediate change)."
    ),
    "both_signal": (
        "**Pre-registered interpretation (SIGNAL on both profiles)**: both "
        "the form4 and sc13d interpretations above apply -- consistent with "
        "the literature for insider purchases, and with activist campaigns "
        "paying out long for sc13d. Either would justify (at most) a new "
        "deployment-design phase with fresh gates; nothing is built or "
        "traded on this verdict alone."
    ),
}


def _select_interpretation(verdicts: list[ProfileVerdict]) -> str:
    is_signal = {
        v.profile: v.decision.endswith("SIGNAL") and not v.decision.endswith("NO-SIGNAL")
        for v in verdicts
    }
    form4_signal = is_signal.get("form4", False)
    sc13d_signal = is_signal.get("sc13d", False)

    if form4_signal and sc13d_signal:
        return INTERPRETATIONS["both_signal"]
    if form4_signal:
        return INTERPRETATIONS["form4_signal"]
    if sc13d_signal:
        return INTERPRETATIONS["sc13d_signal"]
    return INTERPRETATIONS["both_no_signal"]


def render_report(verdicts: list[ProfileVerdict], n_signals: dict[str, int]) -> str:
    lines: list[str] = []
    lines.append("# Study 2 -- Long-Horizon Information Content Report")
    lines.append("")
    lines.append(
        "> Knowledge-question report (SIGNAL / NO-SIGNAL), not a GO/KILL "
        "gate. Spec: `docs/study2-long-horizon-insider.md` (commit "
        "0faa4ac). Phase 0 gate code and that spec are unmodified."
    )
    lines.append("")
    lines.append(SPEC_SUMMARY)
    lines.append("")

    for v in verdicts:
        n_sigs = n_signals.get(v.profile, 0)
        lines.append(f"## Profile: {v.profile} -- **{v.decision}**")
        lines.append("")
        lines.append(f"Signals: {n_sigs}. Coverage: {v.coverage_rate:.1%} "
                      f"(threshold {MIN_COVERAGE:.0%}).")
        if v.passing_horizons:
            lines.append(f"Passing horizon(s): {', '.join(str(h) for h in v.passing_horizons)}.")
        else:
            lines.append("No horizon passed all five criteria.")
        lines.append("")
        lines.append(
            "| Horizon | n | mean excess | 99% CI | floor | floor met | "
            "pos. vintage years | n>=min | CI99 lo>0 | IWM mean | IWM 99% CI |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for hr in v.horizon_results:
            min_n = MIN_N.get(v.profile, 0)
            n_ok = "yes" if hr.n >= min_n else "no"
            ci_ok = "yes" if (not pd.isna(hr.ci_lo) and hr.ci_lo > 0) else "no"
            floor_ok = "yes" if (not pd.isna(hr.mean) and hr.mean >= hr.floor) else "no"
            lines.append(
                f"| {hr.horizon} | {hr.n} | {_fmt_pct(hr.mean)} | "
                f"{_fmt_ci_pct(hr.ci_lo, hr.ci_hi)} | {hr.floor:+.1%} | "
                f"{floor_ok} | {hr.positive_years}/{hr.total_years} | {n_ok} | "
                f"{ci_ok} | {_fmt_pct(hr.iwm_mean)} | "
                f"{_fmt_ci_pct(hr.iwm_ci_lo, hr.iwm_ci_hi)} |"
            )
        lines.append("")

        # Verdict reasons
        lines.append("Per-horizon checks:")
        lines.append("")
        for hr in v.horizon_results:
            status = "PASS (all criteria)" if hr.passed else "fail"
            failed = [k for k, ok in hr.checks.items() if not ok]
            detail = "" if hr.passed else f" -- failed: {', '.join(failed)}"
            lines.append(f"- horizon {hr.horizon}: {status}{detail}")
        lines.append("")

        # IWM-negative flag for any passing horizon (spec requirement)
        flagged = [hr for hr in v.horizon_results if hr.iwm_negative_flag]
        if flagged:
            lines.append("> **FLAG (per spec)**: the following passing "
                          "horizon(s) have a NEGATIVE IWM-relative mean "
                          "excess, suggesting size-regime exposure rather "
                          "than stock selection:")
            for hr in flagged:
                lines.append(
                    f"> - horizon {hr.horizon}: IWM mean {_fmt_pct(hr.iwm_mean)} "
                    f"(CI {_fmt_ci_pct(hr.iwm_ci_lo, hr.iwm_ci_hi)}) vs SPY mean "
                    f"{_fmt_pct(hr.mean)}"
                )
            lines.append("")

        if v.coverage_rate < MIN_COVERAGE:
            lines.append(
                f"> **SUSPECT**: price coverage ({v.coverage_rate:.1%}) is "
                f"below the {MIN_COVERAGE:.0%} threshold (criterion 5); the "
                f"decision above carries a SUSPECT- prefix regardless of "
                f"the other four criteria."
            )
            lines.append("")

    lines.append("## Interpretation")
    lines.append("")
    lines.append(_select_interpretation(verdicts))
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **Realized, not predictive**: these are realized historical "
        "returns over the study window; they do not guarantee future "
        "performance, and the study window includes only one full market "
        "cycle at most."
    )
    lines.append(
        "- **Overlapping windows**: the bootstrap CI resamples per-event "
        "excess returns independently (IID assumption). Events with "
        "overlapping 60/125/250-trading-day holding windows share "
        "market-regime exposure, so the true CI is wider than reported -- "
        "this is part of why the spec uses a 99% (not 95%) interval for "
        "this study."
    )
    lines.append(
        "- **250-day horizon shrinkage**: signals after ~April 2025 cannot "
        "complete a 250-day horizon within the available price history, so "
        "completed-event counts shrink with horizon (per-horizon minimums "
        "in criterion 1 account for this, but smaller n means wider CIs "
        "and noisier vintage-year breakdowns)."
    )
    lines.append(
        "- **IWM diagnostic only**: per spec, the IWM (Russell 2000 ETF) "
        "comparison is a labeled secondary diagnostic and cannot, by "
        "itself, turn a NO-SIGNAL into a SIGNAL or vice versa."
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config(REPO_ROOT / "config.yaml")

    print("=" * 78)
    print("Study 2: long-horizon information content (form4, sc13d)")
    print("Spec: docs/study2-long-horizon-insider.md (commit 0faa4ac)")
    print("Knowledge question (SIGNAL/NO-SIGNAL); does not modify the Phase 0 gate.")
    print("=" * 78)
    print(
        "RUNTIME NOTE: price windows extend ~590 calendar days past each "
        "trigger_date (90 + 2*250) to cover the 250-day exit -- different "
        "from the Phase 0 cache keys (which only need ~170 days for "
        "horizon<=40). Most tickers will be CACHE MISSES on first run "
        "(thousands of fresh price fetches); expect a long first run. "
        "Re-runs against a warm cache are fast and deterministic."
    )

    signals = load_signals(cfg)
    n_signals = {
        "form4": sum(1 for s in signals if s.profile == "form4"),
        "sc13d": sum(1 for s in signals if s.profile == "sc13d"),
    }
    print(f"\nLoaded {len(signals)} signals "
          f"({n_signals['form4']} form4, {n_signals['sc13d']} sc13d)")

    provider = make_provider(cfg)

    print(f"\nRunning event study (benchmark=SPY, horizons={HORIZONS}, "
          f"cost_bps={[COST_BPS]})...")
    events_spy = run_event_study(
        signals, provider, cfg.tradability,
        horizons=HORIZONS, cost_bps_list=[COST_BPS], benchmark="SPY",
    )
    spy_path = cfg.data_dir / "study2_events_spy.parquet"
    spy_path.parent.mkdir(parents=True, exist_ok=True)
    events_spy.to_parquet(spy_path)
    print(f"  wrote {len(events_spy)} rows to {spy_path}")

    print(f"\nRunning event study (benchmark=IWM, horizons={HORIZONS}, "
          f"cost_bps={[COST_BPS]})...")
    print("  (ticker frames are reused from the cache populated by the SPY "
          "pass; only the IWM benchmark frame is fetched fresh)")
    events_iwm = run_event_study(
        signals, provider, cfg.tradability,
        horizons=HORIZONS, cost_bps_list=[COST_BPS], benchmark="IWM",
    )
    iwm_path = cfg.data_dir / "study2_events_iwm.parquet"
    events_iwm.to_parquet(iwm_path)
    print(f"  wrote {len(events_iwm)} rows to {iwm_path}")

    coverage_path = cfg.data_dir / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))

    verdicts = evaluate_study2(events_spy, events_iwm, coverage)

    print("\n" + "-" * 78)
    print("Verdicts")
    print("-" * 78)
    for v in verdicts:
        print(f"\n{v.profile}: {v.decision} "
              f"(coverage {v.coverage_rate:.1%}, "
              f"passing horizons: {v.passing_horizons or 'none'})")
        for hr in v.horizon_results:
            status = "PASS" if hr.passed else "fail"
            mean_s = "n/a" if pd.isna(hr.mean) else f"{hr.mean:+.4f}"
            ci_s = ("n/a" if pd.isna(hr.ci_lo) or pd.isna(hr.ci_hi)
                    else f"[{hr.ci_lo:+.4f}, {hr.ci_hi:+.4f}]")
            iwm_s = "n/a" if hr.iwm_mean is None else f"{hr.iwm_mean:+.4f}"
            print(f"  h={hr.horizon:>3}  n={hr.n:>5}  mean={mean_s:>8}  "
                  f"CI99={ci_s:>22}  pos_years={hr.positive_years}/{hr.total_years}  "
                  f"IWM_mean={iwm_s:>8}  -> {status}")
            if hr.iwm_negative_flag:
                print(f"    FLAG: horizon {hr.horizon} passes but IWM-relative "
                      f"mean is NEGATIVE ({iwm_s}) -- possible size-regime "
                      f"exposure, not stock selection.")

    md = render_report(verdicts, n_signals)
    out_path = cfg.data_dir / "study2-report.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
