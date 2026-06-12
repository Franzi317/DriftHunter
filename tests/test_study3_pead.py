"""Fixture-level tests for scripts/study3_pead.py.

No network, no provider, no SF1/config I/O: builds hand-constructed
SHARADAR/SF1-shaped frames and feeds them to `compute_sue_events` (the SUE
math, pure), and synthetic event-study frames to `evaluate_study3` (the
five pre-committed Study 3 criteria from
`docs/study3-pead.md`, commit 991e34d).
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from study3_pead import (  # noqa: E402
    compute_sue_events,
    evaluate_study3,
    HORIZONS,
    COST_BPS,
    SUE_THRESHOLD,
    WINDOW_START,
    WINDOW_END,
)


# ---------------------------------------------------------------------------
# SF1 fixture helper
# ---------------------------------------------------------------------------

def _sf1_rows(ticker: str, calendardates: list[str], eps: list[float],
              datekey_offset_days: int = 45) -> pd.DataFrame:
    """Build a minimal SHARADAR/SF1 ARQ frame for one ticker: one row per
    (calendardate, eps), with datekey = calendardate + datekey_offset_days.
    `epsdil` carries the value; `eps` (the fallback column) is left null so
    the eps_series = epsdil.fillna(eps) path is exercised on the primary
    field.
    """
    cal = pd.to_datetime(calendardates)
    datekey = cal + pd.Timedelta(days=datekey_offset_days)
    return pd.DataFrame({
        "ticker": ticker,
        "calendardate": cal,
        "datekey": datekey,
        "epsdil": eps,
        "eps": [float("nan")] * len(eps),
    })


def _quarters(start: str, periods: int) -> list[str]:
    """Quarter-end calendardates as ISO strings, starting at `start`."""
    return [d.date().isoformat() for d in pd.date_range(start, periods=periods, freq="QE")]


# ---------------------------------------------------------------------------
# 1. SUE math (hand-calc)
# ---------------------------------------------------------------------------

def test_sue_hand_calc():
    """13 quarters (2018Q1..2021Q1), EPS = [1,1,1,1,2,3,2,3,3,5,3,5,6].

    Seasonal diffs d_q = eps_q - eps_{q-4} for q=4..12 (by position):
        d4=2-1=1   d5=3-1=2   d6=2-1=1   d7=3-1=2
        d8=3-2=1   d9=5-3=2   d10=3-2=1  d11=5-3=2   d12=6-3=3

    For the LAST quarter (position 12, calendardate 2021-03-31), the
    trailing window is d4..d11 (8 diffs, EXCLUDING d12 itself):
        [1, 2, 1, 2, 1, 2, 1, 2]
    mean = 1.5; sum((x-mean)^2) = 4*(0.5)^2 + 4*(0.5)^2 = 2.0
    std(ddof=1) = sqrt(2.0 / 7) = sqrt(2/7) ~= 0.5345224838248488

    SUE_12 = d12 / std = 3 / 0.5345224838248488 ~= 5.612486080160912

    >= 2.0 -> event. datekey = 2021-03-31 + 45d = 2021-05-15, inside
    [2021-04-01, 2026-03-31] -> kept.

    All q-4 gaps are 365 or 366 calendar days (quarter-end dates one year
    apart), within the [350, 380] guard, so every d_q above is valid.
    """
    eps = [1, 1, 1, 1, 2, 3, 2, 3, 3, 5, 3, 5, 6]
    cal = _quarters("2018-03-31", 13)
    sf1 = _sf1_rows("AAA", cal, eps)

    events = compute_sue_events(sf1, WINDOW_START, WINDOW_END)

    assert len(events) == 1
    row = events.iloc[0]
    assert row["ticker"] == "AAA"
    assert row["calendardate"] == pd.Timestamp("2021-03-31")
    assert row["datekey"] == date(2021, 5, 15)

    expected_std = np.std([1, 2, 1, 2, 1, 2, 1, 2], ddof=1)
    assert expected_std == pytest.approx(0.5345224838248488)
    expected_sue = 3 / expected_std
    assert expected_sue == pytest.approx(5.612486080160912)
    assert row["sue"] == pytest.approx(expected_sue)
    assert row["sue"] >= SUE_THRESHOLD


# ---------------------------------------------------------------------------
# 2. Min-history guard
# ---------------------------------------------------------------------------

def test_min_history_five_trailing_diffs_no_event():
    """10 quarters: d4..d8 are the only seasonal diffs computable (5 of
    them), all preceding position 9. At position 9, the trailing window is
    d4..d8 -- 5 non-null diffs, below the MIN_TRAILING_DIFFS=6 requirement
    -- so SUE_9 is not computed, regardless of how large d9 itself is.

    eps chosen so d9 (= eps9 - eps5) would be huge (SUE >> 2 if it were
    computed): eps5=1, eps9=100 -> d9=99.
    """
    eps = [1, 1, 1, 1, 2, 1, 2, 1, 2, 100]
    cal = _quarters("2018-03-31", 10)
    sf1 = _sf1_rows("BBB", cal, eps)

    events = compute_sue_events(sf1, WINDOW_START, WINDOW_END)

    assert events.empty


# ---------------------------------------------------------------------------
# 3. Quarter-gap guard
# ---------------------------------------------------------------------------

def test_quarter_gap_guard_skips_diff_spanning_missing_quarters():
    """Ticker missing 2 consecutive quarters (2020Q1, 2020Q2). The
    seasonal diff at the position immediately following the gap that would
    pair with the pre-gap quarter 4-positions-back spans ~547-550 calendar
    days (outside the [350, 380] guard) and is therefore NOT computed, even
    though the naive (gap-ignoring) jump in eps would be enormous.

    Calendardates present (16 generated, dropping the 2 in the gap ->
    14 rows, re-indexed 0..13):
      0:2018-03-31 1:2018-06-30 2:2018-09-30 3:2018-12-31
      4:2019-03-31 5:2019-06-30 6:2019-09-30 7:2019-12-31
      [2020-03-31, 2020-06-30 MISSING]
      8:2020-09-30 9:2020-12-31 10:2021-03-31 11:2021-06-30
      12:2021-09-30 13:2021-12-31

    new-position-11 (calendardate 2021-06-30) would naively pair with
    new-position-7 (2019-12-31): gap = 547 days -> guard excludes it
    (d11 = NaN), so no event is produced for its datekey (2021-08-14),
    even though eps11=100 vs eps7=1 would otherwise be a massive jump.
    """
    all_cal = _quarters("2018-03-31", 16)
    # drop the 2 quarters at the gap (old positions 8, 9 == 2020-03-31, 2020-06-30)
    cal = all_cal[:8] + all_cal[10:]
    assert len(cal) == 14

    # eps: old positions 0..7 unchanged, old 10..15 -> new 8..13
    eps = [1, 1, 1, 1, 2, 1, 2, 1,   # positions 0..7 (eps7 = 1)
           2, 1, 2, 100, 2, 1]       # positions 8..13 (new-position-11 -> eps=100)
    sf1 = _sf1_rows("CCC", cal, eps)

    events = compute_sue_events(sf1, WINDOW_START, WINDOW_END)

    gapped_datekey = date(2021, 6, 30) + timedelta(days=45)
    assert gapped_datekey == date(2021, 8, 14)
    assert not ((events["ticker"] == "CCC") & (events["datekey"] == gapped_datekey)).any()


# ---------------------------------------------------------------------------
# 4. SUE threshold + window filter
# ---------------------------------------------------------------------------

def test_threshold_and_window_filter():
    """Same 13-quarter shape as the hand-calc test (trailing diffs
    [1,2,1,2,1,2,1,2], std ~= 0.5345224838248488), but the LAST quarter's
    eps is tuned so d12/std lands just below/above SUE_THRESHOLD=2.0:

      SUE=1.9: d12 = 1.9 * std ~= 1.0155927192672127 -> eps12 = 3 + d12
               ~= 4.015592719267213  -> no event (SUE < 2.0)
      SUE=2.1: d12 = 2.1 * std ~= 1.1224972160321824 -> eps12
               ~= 4.122497216032182 -> event (SUE >= 2.0)

    A third ticker repeats the SUE=2.1 shape but with datekey shifted
    outside the study window (datekey < WINDOW_START) -> filtered out by
    the datekey window check despite SUE >= 2.0.
    """
    base = [1, 1, 1, 1, 2, 3, 2, 3, 3, 5, 3, 5]  # positions 0..11 (eps8=3)
    std = np.std([1, 2, 1, 2, 1, 2, 1, 2], ddof=1)
    cal = _quarters("2018-03-31", 13)

    # SUE = 1.9 -> no event
    eps_below = base + [3 + 1.9 * std]
    sf1_below = _sf1_rows("DDD", cal, eps_below)
    events_below = compute_sue_events(sf1_below, WINDOW_START, WINDOW_END)
    assert events_below.empty

    # SUE = 2.1 -> event, datekey 2021-05-15 (inside window)
    eps_above = base + [3 + 2.1 * std]
    sf1_above = _sf1_rows("EEE", cal, eps_above)
    events_above = compute_sue_events(sf1_above, WINDOW_START, WINDOW_END)
    assert len(events_above) == 1
    assert events_above.iloc[0]["sue"] == pytest.approx(2.1)
    assert events_above.iloc[0]["datekey"] == date(2021, 5, 15)

    # Same SUE=2.1 shape, but shift all calendardates back 2 years so
    # datekey falls before WINDOW_START -> filtered out.
    cal_early = _quarters("2016-03-31", 13)
    sf1_early = _sf1_rows("FFF", cal_early, eps_above)
    events_early = compute_sue_events(sf1_early, WINDOW_START, WINDOW_END)
    assert events_early.empty


# ---------------------------------------------------------------------------
# 5. Restatement dedupe
# ---------------------------------------------------------------------------

def test_restatement_dedupe_keeps_latest_datekey():
    """Same 13-quarter hand-calc shape (event at position 12, SUE ~= 5.6125
    when eps12=6). Inject a DUPLICATE row for (ticker, calendardate=
    2021-03-31) with an EARLIER datekey and eps12_old=3.0 (-> d12=0 ->
    SUE=0, no event).

    If dedup kept the earlier-datekey (stale) row, eps12 would be 3.0 and
    no event would be produced. The correct behavior (keep latest datekey)
    keeps eps12=6.0, reproducing the hand-calc event (sue ~= 5.6125) at
    datekey 2021-05-15.
    """
    eps = [1, 1, 1, 1, 2, 3, 2, 3, 3, 5, 3, 5, 6]
    cal = _quarters("2018-03-31", 13)
    sf1 = _sf1_rows("GGG", cal, eps)

    # Stale restatement row: same calendardate as the last quarter, earlier
    # datekey, old (lower) eps.
    stale = pd.DataFrame({
        "ticker": ["GGG"],
        "calendardate": [pd.Timestamp("2021-03-31")],
        "datekey": [pd.Timestamp("2021-03-31") + pd.Timedelta(days=10)],  # earlier than +45
        "epsdil": [3.0],
        "eps": [float("nan")],
    })
    sf1_with_dupe = pd.concat([sf1, stale], ignore_index=True)

    events = compute_sue_events(sf1_with_dupe, WINDOW_START, WINDOW_END)

    assert len(events) == 1
    row = events.iloc[0]
    assert row["datekey"] == date(2021, 5, 15)
    assert row["sue"] == pytest.approx(5.612486080160912)


# ---------------------------------------------------------------------------
# 6. evaluate_study3 criteria
# ---------------------------------------------------------------------------

EVENT_COLUMNS = [
    "profile", "ticker", "trigger_date", "score", "horizon", "cost_bps",
    "entry_date", "exit_date", "raw_return", "excess_return", "filter_reason",
]


def _make_events(horizon_means: dict[int, float], n: int,
                  horizons: list[int] = HORIZONS, cost_bps: int = COST_BPS,
                  noise: float = 0.001, n_years: int = 5) -> pd.DataFrame:
    """Build a synthetic completed-events frame for the "pead" profile.

    `horizon_means` maps horizon -> mean excess_return. Trigger dates
    (== datekey for pead signals) are spread evenly across `n_years`
    consecutive calendar years (2021..2025) so each year gets roughly
    n / n_years rows with the SAME mean (a strongly positive mean ->
    all years positive, satisfying criterion 4).
    """
    rng = np.random.default_rng(7)
    years = [2021 + (i % n_years) for i in range(n)]
    dates = pd.to_datetime([f"{y}-{(i % 12) + 1:02d}-15" for i, y in enumerate(years)])
    entry = dates + pd.Timedelta(days=1)

    frames = []
    for h in horizons:
        mean = horizon_means.get(h, 0.0)
        frames.append(pd.DataFrame({
            "profile": "pead",
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


def _empty_events() -> pd.DataFrame:
    return pd.DataFrame(columns=EVENT_COLUMNS)


def test_signal_case_passes_all_five_criteria():
    """n=600 @ h=5, mean +2% (tight => CI99 lo > 0, well above the +0.5%
    floor), 5 positive years, coverage 0.9 -> SIGNAL with horizon 5 in
    passing_horizons."""
    events_spy = _make_events({5: 0.02, 20: 0.0, 60: 0.0}, n=600, noise=0.001)
    events_iwm = _empty_events()

    verdict = evaluate_study3(events_spy, events_iwm, coverage_rate=0.9)

    assert verdict.decision == "SIGNAL"
    assert 5 in verdict.passing_horizons

    h5 = next(hr for hr in verdict.horizon_results if hr.horizon == 5)
    assert h5.n == 600
    assert h5.ci_lo > 0
    assert h5.mean >= 0.005  # economic floor for h=5
    assert h5.positive_years >= 3
    assert h5.passed


def test_below_economic_floor_is_no_signal():
    """Same shape, but h=5 mean is +0.3% (below the +0.5% floor) ->
    NO-SIGNAL (criterion 3 fails at every horizon)."""
    events_spy = _make_events({5: 0.003, 20: 0.0, 60: 0.0}, n=600, noise=0.001)
    events_iwm = _empty_events()

    verdict = evaluate_study3(events_spy, events_iwm, coverage_rate=0.9)

    assert verdict.decision == "NO-SIGNAL"
    assert verdict.passing_horizons == []

    h5 = next(hr for hr in verdict.horizon_results if hr.horizon == 5)
    # CI should still be tight and positive (n=600, noise=0.001)...
    assert h5.ci_lo > 0
    # ...but the mean is below the +0.5% floor, so the horizon fails overall.
    assert h5.mean < 0.005
    assert not h5.checks["mean>=+0.5%"]
    assert not h5.passed


def test_n_below_500_is_no_signal():
    """n=400 (< MIN_N=500) at every horizon, with a strong mean ->
    NO-SIGNAL via criterion 1 even though the mean/CI/years would
    otherwise pass."""
    events_spy = _make_events({5: 0.05, 20: 0.05, 60: 0.05}, n=400, noise=0.001)
    events_iwm = _empty_events()

    verdict = evaluate_study3(events_spy, events_iwm, coverage_rate=0.9)

    assert verdict.decision == "NO-SIGNAL"
    for hr in verdict.horizon_results:
        assert not hr.checks["n>=500"]
        assert not hr.passed


def test_low_coverage_adds_suspect_prefix():
    """Otherwise-SIGNAL shape with coverage 0.5 (< 0.80 threshold) ->
    SUSPECT-SIGNAL."""
    events_spy = _make_events({5: 0.02, 20: 0.0, 60: 0.0}, n=600, noise=0.001)
    events_iwm = _empty_events()

    verdict = evaluate_study3(events_spy, events_iwm, coverage_rate=0.5)

    assert verdict.decision == "SUSPECT-SIGNAL"


def test_low_coverage_suspect_no_signal():
    """Otherwise-NO-SIGNAL shape (flat everywhere) with coverage 0.5 ->
    SUSPECT-NO-SIGNAL."""
    events_spy = _make_events({5: 0.0, 20: 0.0, 60: 0.0}, n=600, noise=0.001)
    events_iwm = _empty_events()

    verdict = evaluate_study3(events_spy, events_iwm, coverage_rate=0.5)

    assert verdict.decision == "SUSPECT-NO-SIGNAL"
