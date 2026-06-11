"""Fixture-level smoke test for scripts/study2_long_horizon.py.

No network, no provider, no config I/O: builds synthetic event-study frames
matching `run_event_study`'s RESULT_COLUMNS schema and feeds them straight
to `evaluate_study2`, the pure criteria-evaluation function. Exercises the
five pre-committed Study 2 criteria (n minimums, 99% bootstrap CI, economic
floors, vintage-year breadth, coverage) from
`docs/study2-long-horizon-insider.md` (commit 0faa4ac).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from study2_long_horizon import evaluate_study2, HORIZONS, COST_BPS  # noqa: E402


def _make_events(profile: str, horizon_means: dict[int, float], n: int,
                  horizons: list[int] = HORIZONS, cost_bps: int = COST_BPS,
                  noise: float = 0.001, n_years: int = 5) -> pd.DataFrame:
    """Build a synthetic completed-events frame for one profile.

    `horizon_means` maps horizon -> mean excess_return. Trigger dates are
    spread evenly across `n_years` consecutive calendar years (2021..2025)
    so each year gets roughly n / n_years rows with the SAME mean (so a
    strongly positive mean -> all years positive, satisfying criterion 4).
    """
    rng = np.random.default_rng(7)
    years = [2021 + (i % n_years) for i in range(n)]
    dates = pd.to_datetime([f"{y}-{(i % 12) + 1:02d}-15" for i, y in enumerate(years)])
    entry = dates + pd.Timedelta(days=1)

    frames = []
    for h in horizons:
        mean = horizon_means.get(h, 0.0)
        frames.append(pd.DataFrame({
            "profile": profile,
            "ticker": [f"T{i}" for i in range(n)],
            "trigger_date": dates,
            "score": 2.0,
            "horizon": h,
            "cost_bps": cost_bps,
            "entry_date": entry,
            "exit_date": entry + pd.Timedelta(days=h),
            "raw_return": rng.normal(mean, noise, n),
            "excess_return": rng.normal(mean, noise, n),
            "filter_reason": "",
        }))
    return pd.concat(frames, ignore_index=True)


def _empty_iwm() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "profile", "ticker", "trigger_date", "score", "horizon", "cost_bps",
        "entry_date", "exit_date", "raw_return", "excess_return", "filter_reason",
    ])


COVERAGE_GOOD = {
    "form4": {"total": 100, "covered": 90, "missing": [], "rate": 0.90},
    "sc13d": {"total": 100, "covered": 90, "missing": [], "rate": 0.90},
}


def test_strong_60d_signal_passes_all_five_criteria():
    """form4, n=400 @ 60d, mean +2% (tight => CI99 lo > 0), 5 positive
    years, coverage 0.9 -> SIGNAL with horizon 60 in passing_horizons."""
    events_spy = _make_events("form4", {60: 0.02, 125: 0.0, 250: 0.0}, n=400, noise=0.001)
    events_iwm = _empty_iwm()

    verdicts = evaluate_study2(events_spy, events_iwm, COVERAGE_GOOD)
    form4 = next(v for v in verdicts if v.profile == "form4")

    assert form4.decision == "SIGNAL"
    assert 60 in form4.passing_horizons

    h60 = next(hr for hr in form4.horizon_results if hr.horizon == 60)
    assert h60.n == 400
    assert h60.ci_lo > 0
    assert h60.mean >= 0.01  # economic floor for 60d
    assert h60.positive_years >= 3
    assert h60.passed


def test_below_economic_floor_is_no_signal():
    """Same shape, but 60d mean is +0.5% (below the +1.0% floor) ->
    NO-SIGNAL (criterion 3 fails at every horizon)."""
    events_spy = _make_events("form4", {60: 0.005, 125: 0.0, 250: 0.0}, n=400, noise=0.001)
    events_iwm = _empty_iwm()

    verdicts = evaluate_study2(events_spy, events_iwm, COVERAGE_GOOD)
    form4 = next(v for v in verdicts if v.profile == "form4")

    assert form4.decision == "NO-SIGNAL"
    assert form4.passing_horizons == []

    h60 = next(hr for hr in form4.horizon_results if hr.horizon == 60)
    # CI should still be tight and positive (n=400, noise=0.001)...
    assert h60.ci_lo > 0
    # ...but the mean is below the +1.0% floor, so the horizon fails overall.
    assert h60.mean < 0.01
    assert not h60.checks["mean>=+1.0%"]
    assert not h60.passed


def test_low_coverage_adds_suspect_prefix():
    """Otherwise-SIGNAL profile with coverage 0.5 (< 0.80 threshold) ->
    SUSPECT-SIGNAL; otherwise-NO-SIGNAL profile -> SUSPECT-NO-SIGNAL."""
    coverage_bad = {
        "form4": {"total": 100, "covered": 50, "missing": [], "rate": 0.50},
        "sc13d": {"total": 100, "covered": 50, "missing": [], "rate": 0.50},
    }

    # form4: strong 60d signal (would be SIGNAL on coverage alone)
    form4_events = _make_events("form4", {60: 0.02, 125: 0.0, 250: 0.0}, n=400, noise=0.001)
    # sc13d: flat everywhere (would be NO-SIGNAL on coverage alone)
    sc13d_events = _make_events("sc13d", {60: 0.0, 125: 0.0, 250: 0.0}, n=200, noise=0.001)

    events_spy = pd.concat([form4_events, sc13d_events], ignore_index=True)
    events_iwm = _empty_iwm()

    verdicts = evaluate_study2(events_spy, events_iwm, coverage_bad)
    by_profile = {v.profile: v for v in verdicts}

    assert by_profile["form4"].decision == "SUSPECT-SIGNAL"
    assert by_profile["sc13d"].decision == "SUSPECT-NO-SIGNAL"


def test_iwm_negative_flag_on_passing_horizon():
    """A passing horizon whose IWM-relative mean is negative must be
    flagged via `iwm_negative_flag`, without changing the verdict."""
    events_spy = _make_events("form4", {60: 0.02, 125: 0.0, 250: 0.0}, n=400, noise=0.001)
    events_iwm = _make_events("form4", {60: -0.01, 125: 0.0, 250: 0.0}, n=400, noise=0.001)

    verdicts = evaluate_study2(events_spy, events_iwm, COVERAGE_GOOD)
    form4 = next(v for v in verdicts if v.profile == "form4")

    assert form4.decision == "SIGNAL"
    h60 = next(hr for hr in form4.horizon_results if hr.horizon == 60)
    assert h60.passed
    assert h60.iwm_mean is not None and h60.iwm_mean < 0
    assert h60.iwm_negative_flag is True


def test_insufficient_n_is_no_signal():
    """sc13d with n below the 150 minimum at every horizon -> NO-SIGNAL via
    criterion 1, even with a strong mean."""
    events_spy = _make_events("sc13d", {60: 0.05, 125: 0.05, 250: 0.05}, n=100, noise=0.001)
    events_iwm = _empty_iwm()

    verdicts = evaluate_study2(events_spy, events_iwm, COVERAGE_GOOD)
    sc13d = next(v for v in verdicts if v.profile == "sc13d")

    assert sc13d.decision == "NO-SIGNAL"
    for hr in sc13d.horizon_results:
        assert not hr.checks["n>=150"]
        assert not hr.passed
