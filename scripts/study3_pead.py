"""Study 3 -- Post-Earnings-Announcement Drift (PEAD), filing-date anchored.

Spec: `docs/study3-pead.md` (commit 991e34d, criteria frozen BEFORE this
script computed any return statistic). This script does NOT modify engine
code, prior studies, or that spec. It is a standalone evaluation re-using
`run_event_study` exactly as `drifthunter study` does, at horizons
[5, 20, 60] / cost 30bp / benchmark SPY (primary) and IWM (labeled secondary
diagnostic only -- per spec it cannot flip a verdict).

Verdict labels are SIGNAL / NO-SIGNAL (a knowledge question), not GO/KILL.

Pipeline:
    1. Pull SHARADAR/SF1 (dimension ARQ) per calendar year, 2014..present,
       caching each year to data/sf1/arq_{year}.parquet.
    2. Compute SUE events (pure function, `compute_sue_events`).
    3. Assemble Signal(profile="pead", ...) rows, apply the exchange filter
       and derivative-listing screen (same rules as `drifthunter signals`),
       save data/study3_signals.parquet.
    4. Run the event study (SPY primary, IWM diagnostic), compute coverage,
       evaluate the five pre-committed criteria, render data/study3-report.md.

Outputs:
    data/sf1/arq_{year}.parquet      (intermediate; gitignored; one per year)
    data/study3_signals.parquet
    data/study3_events_spy.parquet
    data/study3_events_iwm.parquet
    data/study3-report.md

Usage:
    uv run python scripts/study3_pead.py

Runtime note: this is a COLD-START pipeline -- the SF1 pull alone is
~12 years x ~14k tickers x 4 quarters, and the event study then fetches
prices for every (ticker, datekey) event with ~590 calendar days of
lookahead (90 + 2*60). Expect a long first run; re-runs against warm SF1 +
price caches are fast and fully deterministic.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Sharadar key comes from the environment. Cached SF1/price reads never touch
# the network, so a re-run against warm caches works without any key.
if not os.environ.get("NASDAQ_DATA_LINK_API_KEY"):
    print("NOTE: NASDAQ_DATA_LINK_API_KEY not set; relying on local SF1 and "
          "price caches only.", file=sys.stderr)

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from drifthunter.config import Config, load_config
from drifthunter.ingest.http import EdgarClient
from drifthunter.ingest.tickers import TickerMap
from drifthunter.prices.provider import CachingProvider, coverage_report
from drifthunter.prices.sharadar import SharadarProvider
from drifthunter.prices.free import FreeProvider
from drifthunter.scorer.models import Signal
from drifthunter.backtest.event_study import run_event_study
from drifthunter.backtest.stats import bootstrap_ci, yearly_means
from drifthunter.cli import _looks_like_derivative_listing


# ---------------------------------------------------------------------------
# Frozen criteria (spec section "Pre-committed SIGNAL criteria")
# ---------------------------------------------------------------------------

HORIZONS = [5, 20, 60]
COST_BPS = 30
SEED = 42
N_ITER = 10_000
ALPHA = 0.01  # 99% CI

MIN_N = 500
ECONOMIC_FLOOR = {5: 0.005, 20: 0.010, 60: 0.015}  # decimals (0.5% / 1.0% / 1.5%)
MIN_POSITIVE_YEARS = 3
MIN_COVERAGE = 0.80

SUE_THRESHOLD = 2.0
SUE_CAP = 10.0
WINDOW_START = date(2021, 4, 1)
WINDOW_END = date(2026, 3, 31)
SF1_START_YEAR = 2014


# ---------------------------------------------------------------------------
# 1. SF1 ingest
# ---------------------------------------------------------------------------

def fetch_sf1_arq(api_key: str, start_year: int = SF1_START_YEAR,
                   cache_dir: Path | None = None) -> pd.DataFrame:
    """Pull SHARADAR/SF1 (dimension ARQ), ticker/calendardate/datekey/epsdil/eps,
    for calendardate >= {start_year}-01-01, through the current year.

    `nasdaqdatalink.get_table(..., paginate=True)` caps at ~1M rows. ARQ
    2014+ across ~14k tickers (~60k rows/year) would approach that cap if
    pulled in one call, so this pulls per CALENDAR YEAR (calendardate
    gte/lte that year, paginate=True each) and concatenates. Each year is
    cached to `{cache_dir}/arq_{year}.parquet` (default
    `data/sf1/arq_{year}.parquet`); a cached year is never re-fetched.

    2014 start (vs. the 2021 event window start) gives every event from
    2021 onward >=12 quarters of trailing history (needed for the 8 trailing
    seasonal diffs + the q-4 lookback for the oldest of those), with margin.
    """
    if cache_dir is None:
        cache_dir = REPO_ROOT / "data" / "sf1"
    cache_dir.mkdir(parents=True, exist_ok=True)

    import nasdaqdatalink
    nasdaqdatalink.ApiConfig.api_key = api_key

    columns = ["ticker", "calendardate", "datekey", "epsdil", "eps"]
    frames: list[pd.DataFrame] = []
    end_year = date.today().year

    for year in range(start_year, end_year + 1):
        cache_path = cache_dir / f"arq_{year}.parquet"
        if cache_path.exists():
            frames.append(pd.read_parquet(cache_path))
            continue

        raw = nasdaqdatalink.get_table(
            "SHARADAR/SF1",
            dimension="ARQ",
            calendardate={"gte": f"{year}-01-01", "lte": f"{year}-12-31"},
            qopts={"columns": columns},
            paginate=True,
        )
        if raw is None:
            raw = pd.DataFrame(columns=columns)
        raw = raw[columns].copy()
        raw.to_parquet(cache_path)
        frames.append(raw)

    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# 2. SUE computation (pure -- no I/O)
# ---------------------------------------------------------------------------

EVENT_COLUMNS = ["ticker", "datekey", "calendardate", "sue"]

# A seasonal diff d_q = eps_q - eps_{q-4} is only computed when the q-4 row's
# calendardate is 350-380 days earlier than q's -- i.e. "approximately one
# year, by position" in the per-ticker calendardate-ordered quarterly series.
# This guards against gaps/irregular fiscal calendars corrupting the seasonal
# diff (spec's "seasonal random-walk" intent assumes a clean quarterly
# cadence; a diff spanning a missing quarter or an off-cadence fiscal-year
# change is not a same-season comparison and is excluded).
_MIN_SEASONAL_GAP_DAYS = 350
_MAX_SEASONAL_GAP_DAYS = 380

MIN_TRAILING_DIFFS = 6
TRAILING_WINDOW = 8


def compute_sue_events(sf1: pd.DataFrame, window_start: date, window_end: date
                        ) -> pd.DataFrame:
    """Compute SUE per (ticker, datekey) and return event rows where
    SUE >= SUE_THRESHOLD and window_start <= datekey <= window_end.

    Steps (per spec, docs/study3-pead.md):
      - eps_series = epsdil, falling back to eps where epsdil is null.
      - Per ticker, dedupe (ticker, calendardate) keeping the row with the
        LATEST datekey (restatements refile the same calendardate under a
        later datekey; the latest refiling is the as-reported-at-the-time
        value closest to what the market eventually saw for that quarter --
        we keep one row per fiscal quarter).
      - Order each ticker's remaining rows by calendardate. d_q = eps_q -
        eps_{q-4} BY POSITION in that ordering, only when the q-4 row's
        calendardate is 350-380 days before q's (see _MIN/_MAX_SEASONAL_GAP_DAYS
        above).
      - SUE_q = d_q / std(d_{q-1..q-8}, ddof=1) -- the trailing window is the
        8 diffs immediately preceding q (positions q-1 down to q-8, each
        itself possibly NaN if its own gap guard failed). Requires >=6
        non-null trailing diffs and std > 0 (no peeking at d_q itself).
      - Event rows: SUE >= 2.0, window_start <= datekey <= window_end.
        One row per (ticker, datekey); if duplicates arise (shouldn't, given
        the dedupe above), keep the max SUE.
    """
    if sf1.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    df = sf1.copy()
    df["eps_series"] = df["epsdil"].fillna(df["eps"])
    df["calendardate"] = pd.to_datetime(df["calendardate"])
    df["datekey"] = pd.to_datetime(df["datekey"])

    # Drop rows with no usable EPS at all (both epsdil and eps null) -- they
    # cannot contribute to a seasonal diff in either direction.
    df = df.dropna(subset=["eps_series"])
    if df.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    # Restatement dedupe: keep the row with the latest datekey for each
    # (ticker, calendardate).
    df = df.sort_values(["ticker", "calendardate", "datekey"])
    df = df.drop_duplicates(subset=["ticker", "calendardate"], keep="last")

    event_rows: list[dict] = []

    for ticker, g in df.groupby("ticker", sort=False):
        g = g.sort_values("calendardate").reset_index(drop=True)
        n = len(g)
        cal = g["calendardate"].to_numpy()
        eps = g["eps_series"].to_numpy(dtype=float)

        # d[i] = eps[i] - eps[i-4], gap-guarded.
        d = [float("nan")] * n
        for i in range(4, n):
            gap_days = (cal[i] - cal[i - 4]).astype("timedelta64[D]").astype(int)
            if _MIN_SEASONAL_GAP_DAYS <= gap_days <= _MAX_SEASONAL_GAP_DAYS:
                d[i] = eps[i] - eps[i - 4]

        for i in range(n):
            if pd.isna(d[i]):
                continue
            lo = max(0, i - TRAILING_WINDOW)
            trailing = [x for x in d[lo:i] if not pd.isna(x)]
            if len(trailing) < MIN_TRAILING_DIFFS:
                continue
            std = pd.Series(trailing).std(ddof=1)
            if not (std > 0):
                continue
            sue = d[i] / std
            if sue < SUE_THRESHOLD:
                continue

            datekey = g.loc[i, "datekey"]
            if not (pd.Timestamp(window_start) <= datekey <= pd.Timestamp(window_end)):
                continue

            event_rows.append({
                "ticker": ticker,
                "datekey": datekey,
                "calendardate": g.loc[i, "calendardate"],
                "sue": float(sue),
            })

    if not event_rows:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    events = pd.DataFrame(event_rows, columns=EVENT_COLUMNS)
    # One row per (ticker, datekey); keep max sue on duplicates.
    events = events.sort_values("sue", ascending=False)
    events = events.drop_duplicates(subset=["ticker", "datekey"], keep="first")
    events = events.sort_values(["ticker", "datekey"]).reset_index(drop=True)
    events["datekey"] = events["datekey"].dt.date
    return events


# ---------------------------------------------------------------------------
# Provider stack (mirrors scripts/study2_long_horizon.py)
# ---------------------------------------------------------------------------

class SpyIwmFreeRoutingProvider:
    """Routes both "SPY" and "IWM" to the free provider (Sharadar SEP covers
    equities, not ETFs; the benchmarks have no survivorship concern), all
    other tickers to the configured primary provider.

    Identical to study2's provider of the same name: a single provider that
    routes both benchmark tickers free keeps cache keys consistent across
    the SPY pass and the IWM diagnostic pass (and across the coverage
    report, which uses the same provider).
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
        mean is negative (spec: must be flagged -- size-regime exposure
        rather than stock selection)."""
        return self.passed and self.iwm_mean is not None and self.iwm_mean < 0


@dataclass
class Study3Verdict:
    decision: str  # "SIGNAL" | "NO-SIGNAL", optionally "SUSPECT-" prefixed
    coverage_rate: float
    passing_horizons: list[int]
    horizon_results: list[HorizonResult] = field(default_factory=list)


def _completed(events: pd.DataFrame, horizon: int, cost_bps: int = COST_BPS
               ) -> pd.DataFrame:
    if events.empty:
        return events
    return events[
        (events["horizon"] == horizon)
        & (events["cost_bps"] == cost_bps)
        & (events["filter_reason"] == "")
    ]


def evaluate_study3(events_spy: pd.DataFrame, events_iwm: pd.DataFrame,
                     coverage_rate: float, *,
                     horizons: list[int] = HORIZONS,
                     n_iter: int = N_ITER, seed: int = SEED, alpha: float = ALPHA,
                     ) -> Study3Verdict:
    """Apply the five pre-committed Study 3 criteria to event-study output.

    Pure function: no file I/O, no provider calls. `events_spy`/`events_iwm`
    are `run_event_study` output frames (any cost_bps sweep is fine; only
    `COST_BPS=30` rows are used). `coverage_rate` is the pead-universe price
    coverage rate (0..1).

    Criteria (ALL must hold at >=1 horizon for SIGNAL; spec section
    "Pre-committed SIGNAL criteria"):
      1. n >= 500 completed events at that horizon.
      2. 99% bootstrap CI lower bound > 0 on mean per-event excess return
         after 30bp costs vs SPY (alpha=0.01, seed=42, n_iter=10_000).
      3. Economic floor on the mean excess: >= +0.5% (5d), >= +1.0% (20d),
         >= +1.5% (60d).
      4. Positive mean excess in >=3 of 5 vintage calendar years (by
         datekey, i.e. trigger_date for these "pead" signals).
      5. Price coverage >= 80% (else SUSPECT-* prefix on the decision;
         coverage never changes SIGNAL vs NO-SIGNAL itself).
    """
    horizon_results: list[HorizonResult] = []
    passing_horizons: list[int] = []

    for h in horizons:
        spy_df = _completed(events_spy, h)
        n = len(spy_df)

        if n == 0:
            horizon_results.append(HorizonResult(
                horizon=h, n=0, mean=float("nan"), ci_lo=float("nan"),
                ci_hi=float("nan"), floor=ECONOMIC_FLOOR[h],
                positive_years=0, total_years=0,
                iwm_n=None, iwm_mean=None, iwm_ci_lo=None, iwm_ci_hi=None,
                checks={
                    f"n>={MIN_N}": False,
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
        iwm_df = _completed(events_iwm, h)
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
            f"n>={MIN_N}": n >= MIN_N,
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
    if coverage_rate < MIN_COVERAGE:
        decision = f"SUSPECT-{decision}"

    return Study3Verdict(
        decision=decision, coverage_rate=coverage_rate,
        passing_horizons=passing_horizons, horizon_results=horizon_results,
    )


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
This report was generated by `scripts/study3_pead.py` against the frozen
criteria in `docs/study3-pead.md` (commit 991e34d, criteria committed BEFORE
any Study 3 return statistic was computed). The five SIGNAL criteria below
are restated verbatim from that spec; this script does not alter them.

Pre-committed SIGNAL criteria (ALL must hold at >=1 horizon):

1. n >= 500 completed events at that horizon.
2. 99% bootstrap CI lower bound > 0 on mean per-event excess return after
   30bp costs vs SPY (alpha=0.01; seed 42; 10,000 iterations).
3. Economic significance floor on the mean excess at that horizon:
   >= +0.5% (5d), >= +1.0% (20d), >= +1.5% (60d).
4. Positive mean excess in >= 3 of 5 vintage calendar years (by datekey).
5. Price coverage >= 80% for the pead universe (else verdict is SUSPECT-*).

Failing all horizons on any criterion -> NO-SIGNAL. Descriptive portfolio
statistics may be reported but are explicitly NOT verdict inputs. IWM
(Russell 2000 ETF) figures are a labeled secondary diagnostic only and cannot
flip a verdict; per spec, any SIGNAL accompanied by a NEGATIVE IWM-relative
mean at the same horizon is flagged prominently below.
"""

INTERPRETATIONS = {
    "no_signal": (
        "**Pre-registered interpretation (NO-SIGNAL)**: PEAD, as testable at "
        "SEC-filing-date latency with estimate-free SUE, joins the "
        "filing-drift family: real or not historically, it is not reachable "
        "by this design in this window. The third dead family; expected "
        "base case given Studies 1-2."
    ),
    "signal": (
        "**Pre-registered interpretation (SIGNAL)**: would justify (at "
        "most) a deployment-design phase with fresh gates -- and would "
        "first demand a regime/subsample robustness pass, given the "
        "multiple-testing position of this study."
    ),
}


def _select_interpretation(verdict: Study3Verdict) -> str:
    is_signal = verdict.decision.endswith("SIGNAL") and not verdict.decision.endswith("NO-SIGNAL")
    return INTERPRETATIONS["signal"] if is_signal else INTERPRETATIONS["no_signal"]


def render_report(verdict: Study3Verdict, n_signals: int, n_kept: int,
                   n_dropped: int) -> str:
    lines: list[str] = []
    lines.append("# Study 3 -- Post-Earnings-Announcement Drift (PEAD) Report")
    lines.append("")
    lines.append(
        "> Knowledge-question report (SIGNAL / NO-SIGNAL), not a GO/KILL "
        "gate. Spec: `docs/study3-pead.md` (commit 991e34d). Phase 0 gate "
        "code, prior studies, and that spec are unmodified."
    )
    lines.append("")
    lines.append(SPEC_SUMMARY)
    lines.append("")

    lines.append(f"## Profile: pead -- **{verdict.decision}**")
    lines.append("")
    lines.append(f"Signals: {n_signals} ({n_kept} kept, {n_dropped} dropped by "
                  f"exchange/derivative-listing filters). Coverage: "
                  f"{verdict.coverage_rate:.1%} (threshold {MIN_COVERAGE:.0%}).")
    if verdict.passing_horizons:
        lines.append(f"Passing horizon(s): "
                      f"{', '.join(str(h) for h in verdict.passing_horizons)}.")
    else:
        lines.append("No horizon passed all five criteria.")
    lines.append("")
    lines.append(
        "| Horizon | n | mean excess | 99% CI | floor | floor met | "
        "pos. vintage years | n>=min | CI99 lo>0 | IWM mean | IWM 99% CI |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for hr in verdict.horizon_results:
        n_ok = "yes" if hr.n >= MIN_N else "no"
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

    lines.append("Per-horizon checks:")
    lines.append("")
    for hr in verdict.horizon_results:
        status = "PASS (all criteria)" if hr.passed else "fail"
        failed = [k for k, ok in hr.checks.items() if not ok]
        detail = "" if hr.passed else f" -- failed: {', '.join(failed)}"
        lines.append(f"- horizon {hr.horizon}: {status}{detail}")
    lines.append("")

    flagged = [hr for hr in verdict.horizon_results if hr.iwm_negative_flag]
    if flagged:
        lines.append("> **FLAG (per spec)**: the following passing "
                      "horizon(s) have a NEGATIVE IWM-relative mean excess, "
                      "suggesting size-regime exposure rather than stock "
                      "selection:")
        for hr in flagged:
            lines.append(
                f"> - horizon {hr.horizon}: IWM mean {_fmt_pct(hr.iwm_mean)} "
                f"(CI {_fmt_ci_pct(hr.iwm_ci_lo, hr.iwm_ci_hi)}) vs SPY mean "
                f"{_fmt_pct(hr.mean)}"
            )
        lines.append("")

    if verdict.coverage_rate < MIN_COVERAGE:
        lines.append(
            f"> **SUSPECT**: price coverage ({verdict.coverage_rate:.1%}) is "
            f"below the {MIN_COVERAGE:.0%} threshold (criterion 5); the "
            f"decision above carries a SUSPECT- prefix regardless of the "
            f"other four criteria."
        )
        lines.append("")

    lines.append("## Interpretation")
    lines.append("")
    lines.append(_select_interpretation(verdict))
    lines.append("")
    flagged_any = bool(flagged)
    if flagged_any:
        lines.append(
            "Per spec: any SIGNAL with negative same-horizon IWM-relative "
            "mean must be flagged as size-regime exposure -- see the FLAG "
            "block above."
        )
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **Realized, not predictive**: these are realized historical "
        "returns over the study window; they do not guarantee future "
        "performance, and the study window includes only one full market "
        "cycle at most (one regime)."
    )
    lines.append(
        "- **datekey anchor**: trigger_date = SEC filing datekey, which is "
        "AT OR AFTER the earnings press release. A NO-SIGNAL here does not "
        "rule out press-release-anchored drift; that variant is not "
        "testable with this data."
    )
    lines.append(
        "- **Long-only leg**: this study tests only the long side of "
        "positive-SUE drift. The literature's short leg (negative SUE) is "
        "untested here -- a stated out-of-scope limitation."
    )
    lines.append(
        "- **IID bootstrap on overlapping windows**: the bootstrap CI "
        "resamples per-event excess returns independently (IID assumption). "
        "Events with overlapping 5/20/60-trading-day holding windows share "
        "market-regime exposure, so the true CI is wider than reported -- "
        "this is part of why the spec uses a 99% (not 95%) interval for "
        "this study."
    )
    lines.append(
        "- **One regime / multiple-testing**: this is test #3 on adjacent "
        "data in the same window as Studies 1-2; the bar is held at Study "
        "2's stricter level for this reason. Any SIGNAL would first demand "
        "a regime/subsample robustness pass before any deployment-design "
        "phase."
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
    print("Study 3: post-earnings-announcement drift (PEAD)")
    print("Spec: docs/study3-pead.md (commit 991e34d)")
    print("Knowledge question (SIGNAL/NO-SIGNAL); does not modify the Phase 0 gate.")
    print("=" * 78)

    api_key = os.environ.get(cfg.prices.nasdaq_api_key_env, "")

    # --- 1. SF1 ingest ---
    print(f"\nFetching SHARADAR/SF1 (dimension=ARQ), {SF1_START_YEAR}+ "
          f"(per-year, cached under data/sf1/)...")
    sf1 = fetch_sf1_arq(api_key, start_year=SF1_START_YEAR)
    print(f"  loaded {len(sf1)} ARQ rows")

    # --- 2. SUE events ---
    events = compute_sue_events(sf1, WINDOW_START, WINDOW_END)
    print(f"\nComputed {len(events)} SUE events "
          f"(SUE >= {SUE_THRESHOLD}, datekey in "
          f"[{WINDOW_START}, {WINDOW_END}])")

    # --- 3. Signal assembly ---
    raw_signals = [
        Signal(
            profile="pead",
            ticker=row.ticker,
            trigger_date=row.datekey,
            score=min(row.sue, SUE_CAP),
            detail=f"sue={row.sue:.2f}",
        )
        for row in events.itertuples()
    ]

    cache_dir = cfg.data_dir / "edgar_cache"
    client = EdgarClient(cfg.edgar, cache_dir)
    tickers = TickerMap.fetch(client)

    # SF1-derived signals carry a ticker, not a CIK, so TickerMap's
    # cik-keyed lookups (ticker_for_cik / exchange_for_cik / is_listed) need
    # a one-time reverse index. Built once from the public _by_cik field
    # (cik -> (ticker, exchange)); ~10k entries, negligible cost.
    exchange_by_ticker: dict[str, str] = {
        t: ex for _cik, (t, ex) in tickers._by_cik.items()
    }

    n_kept = 0
    n_dropped_exchange = 0
    kept: list[Signal] = []
    for sig in raw_signals:
        exchange = exchange_by_ticker.get(sig.ticker)
        if exchange is not None and exchange in cfg.tradability.allowed_exchanges:
            kept.append(sig)
            n_kept += 1
        else:
            n_dropped_exchange += 1
    print(f"\npead: kept {n_kept}, dropped {n_dropped_exchange} (exchange filter)")

    n_before = len(kept)
    kept = [s for s in kept if not _looks_like_derivative_listing(s.ticker)]
    n_dropped_derivative = n_before - len(kept)
    print(f"instrument filter: dropped {n_dropped_derivative}")

    n_dropped = n_dropped_exchange + n_dropped_derivative

    sig_df = pd.DataFrame({
        "profile": [s.profile for s in kept],
        "ticker": [s.ticker for s in kept],
        "trigger_date": [pd.Timestamp(s.trigger_date) for s in kept],
        "score": [s.score for s in kept],
        "detail": [s.detail for s in kept],
    })
    sig_path = cfg.data_dir / "study3_signals.parquet"
    sig_path.parent.mkdir(parents=True, exist_ok=True)
    sig_df.to_parquet(sig_path)
    print(f"wrote {len(sig_df)} signals to {sig_path}")

    # --- 4. Event study + evaluation ---
    provider = make_provider(cfg)

    print(f"\nRunning event study (benchmark=SPY, horizons={HORIZONS}, "
          f"cost_bps={[COST_BPS]})...")
    events_spy = run_event_study(
        kept, provider, cfg.tradability,
        horizons=HORIZONS, cost_bps_list=[COST_BPS], benchmark="SPY",
    )
    spy_path = cfg.data_dir / "study3_events_spy.parquet"
    events_spy.to_parquet(spy_path)
    print(f"  wrote {len(events_spy)} rows to {spy_path}")

    print(f"\nRunning event study (benchmark=IWM, horizons={HORIZONS}, "
          f"cost_bps={[COST_BPS]})...")
    print("  (ticker frames are reused from the cache populated by the SPY "
          "pass; only the IWM benchmark frame is fetched fresh)")
    events_iwm = run_event_study(
        kept, provider, cfg.tradability,
        horizons=HORIZONS, cost_bps_list=[COST_BPS], benchmark="IWM",
    )
    iwm_path = cfg.data_dir / "study3_events_iwm.parquet"
    events_iwm.to_parquet(iwm_path)
    print(f"  wrote {len(events_iwm)} rows to {iwm_path}")

    print("\nComputing pead-universe price coverage...")
    pead_tickers = sorted(set(s.ticker for s in kept))
    rep = coverage_report(pead_tickers, provider, WINDOW_START, WINDOW_END)
    print(f"  pead: coverage {rep.covered}/{rep.total} ({rep.rate:.1%})")

    verdict = evaluate_study3(events_spy, events_iwm, rep.rate)

    print("\n" + "-" * 78)
    print("Verdict")
    print("-" * 78)
    print(f"\npead: {verdict.decision} "
          f"(coverage {verdict.coverage_rate:.1%}, "
          f"passing horizons: {verdict.passing_horizons or 'none'})")
    for hr in verdict.horizon_results:
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

    md = render_report(verdict, len(raw_signals), n_kept, n_dropped)
    out_path = cfg.data_dir / "study3-report.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
