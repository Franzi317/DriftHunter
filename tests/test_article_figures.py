"""Fixture-level tests for scripts/article_figures.py (Figure 2: horizon decay).

No network, no provider, no real data: builds tiny synthetic event-study
frames matching `run_event_study`'s RESULT_COLUMNS schema and feeds them
straight to the pure function `compute_horizon_summary`. The render smoke
test only checks that `render_horizon_decay` produces a nonempty PNG file
(headless via matplotlib Agg backend).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from article_figures import compute_horizon_summary, render_horizon_decay  # noqa: E402


def _make_events(profile: str, horizons: list[int], n: int,
                  excess_mean: float, cost_bps: int = 30,
                  noise: float = 0.0005, seed: int = 7) -> pd.DataFrame:
    """Build a synthetic completed-events frame for one profile across
    `horizons`, with `excess_return` drawn from a tight seeded normal
    centered on `excess_mean` (so the mean is deterministic to within
    floating-point tolerance and the bootstrap CI is well-defined)."""
    rng = np.random.default_rng(seed)
    dates = pd.to_datetime([f"2022-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}" for i in range(n)])
    entry = dates + pd.Timedelta(days=1)

    frames = []
    for h in horizons:
        frames.append(pd.DataFrame({
            "profile": profile,
            "ticker": [f"T{i}" for i in range(n)],
            "trigger_date": dates,
            "score": 2.0,
            "horizon": h,
            "cost_bps": cost_bps,
            "entry_date": entry,
            "exit_date": entry + pd.Timedelta(days=h),
            "raw_return": rng.normal(excess_mean, noise, n),
            "excess_return": rng.normal(excess_mean, noise, n),
            "filter_reason": "",
        }))
    return pd.concat(frames, ignore_index=True)


def _events_p0() -> pd.DataFrame:
    """Phase-0-shaped frame: horizons [5, 10], cost_bps=30, two profiles."""
    return pd.concat([
        _make_events("form4", [5, 10], n=40, excess_mean=0.001, seed=1),
        _make_events("sc13d", [5, 10], n=40, excess_mean=-0.01, seed=2),
    ], ignore_index=True)


def _events_s2() -> pd.DataFrame:
    """Study-2-shaped frame: horizon [60], cost_bps=30, two profiles."""
    return pd.concat([
        _make_events("form4", [60], n=40, excess_mean=-0.02, seed=3),
        _make_events("sc13d", [60], n=40, excess_mean=-0.11, seed=4),
    ], ignore_index=True)


def test_row_count_is_profiles_times_horizons_present():
    events_p0 = _events_p0()
    events_s2 = _events_s2()
    summary = compute_horizon_summary(events_p0, events_s2)

    # 2 profiles x 3 horizons present (5, 10, 60)
    assert len(summary) == 6
    assert set(summary["horizon"]) == {5, 10, 60}
    assert set(summary["profile"]) == {"form4", "sc13d"}


def test_ci_level_mapping():
    events_p0 = _events_p0()
    events_s2 = _events_s2()
    summary = compute_horizon_summary(events_p0, events_s2)

    for _, row in summary.iterrows():
        if row["horizon"] in (5, 10):
            assert row["ci_level"] == pytest.approx(0.95)
        elif row["horizon"] == 60:
            assert row["ci_level"] == pytest.approx(0.99)
        else:
            raise AssertionError(f"unexpected horizon {row['horizon']}")


def test_means_match_hand_computed_inputs():
    events_p0 = _events_p0()
    events_s2 = _events_s2()
    summary = compute_horizon_summary(events_p0, events_s2)

    expected = {
        ("form4", 5): events_p0[
            (events_p0["profile"] == "form4") & (events_p0["horizon"] == 5)
        ]["excess_return"].mean(),
        ("form4", 10): events_p0[
            (events_p0["profile"] == "form4") & (events_p0["horizon"] == 10)
        ]["excess_return"].mean(),
        ("sc13d", 5): events_p0[
            (events_p0["profile"] == "sc13d") & (events_p0["horizon"] == 5)
        ]["excess_return"].mean(),
        ("sc13d", 10): events_p0[
            (events_p0["profile"] == "sc13d") & (events_p0["horizon"] == 10)
        ]["excess_return"].mean(),
        ("form4", 60): events_s2[
            (events_s2["profile"] == "form4") & (events_s2["horizon"] == 60)
        ]["excess_return"].mean(),
        ("sc13d", 60): events_s2[
            (events_s2["profile"] == "sc13d") & (events_s2["horizon"] == 60)
        ]["excess_return"].mean(),
    }

    for _, row in summary.iterrows():
        key = (row["profile"], row["horizon"])
        assert row["mean"] == pytest.approx(expected[key], abs=1e-12)
        # CI should bracket the mean
        assert row["ci_lo"] <= row["mean"] <= row["ci_hi"]


def test_deterministic_across_calls():
    events_p0 = _events_p0()
    events_s2 = _events_s2()

    a = compute_horizon_summary(events_p0, events_s2)
    b = compute_horizon_summary(events_p0, events_s2)

    pd.testing.assert_frame_equal(a, b)


def test_filters_to_cost_30_and_completed_only():
    events_p0 = _events_p0()
    events_s2 = _events_s2()

    # Add some rows that should be excluded: other cost_bps and a filter_reason.
    extra = events_p0.copy()
    extra["cost_bps"] = 60
    extra_filtered = events_p0.copy()
    extra_filtered["filter_reason"] = "no_prices"

    events_p0_with_noise = pd.concat([events_p0, extra, extra_filtered], ignore_index=True)

    summary_clean = compute_horizon_summary(events_p0, events_s2)
    summary_noisy = compute_horizon_summary(events_p0_with_noise, events_s2)

    pd.testing.assert_frame_equal(summary_clean, summary_noisy)


def test_deterministic_ordering_sorted_by_profile_then_horizon():
    events_p0 = _events_p0()
    events_s2 = _events_s2()
    summary = compute_horizon_summary(events_p0, events_s2)

    sorted_summary = summary.sort_values(["profile", "horizon"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        summary.reset_index(drop=True), sorted_summary
    )


def test_handles_arbitrary_horizon_sets_not_just_seven():
    """The function must not hardcode 7 horizons -- exercise a smaller,
    different horizon set than the full 5/10/20/40/60/125/250."""
    events_p0 = _make_events("form4", [5], n=40, excess_mean=0.0, seed=11)
    events_s2 = _make_events("form4", [125], n=40, excess_mean=0.0, seed=12)

    summary = compute_horizon_summary(events_p0, events_s2)

    assert len(summary) == 2
    assert set(summary["horizon"]) == {5, 125}
    assert dict(zip(summary["horizon"], summary["ci_level"])) == {5: 0.95, 125: 0.99}


def test_render_horizon_decay_creates_nonempty_png(tmp_path):
    events_p0 = _events_p0()
    events_s2 = _events_s2()
    summary = compute_horizon_summary(events_p0, events_s2)

    out_path = tmp_path / "f.png"
    render_horizon_decay(summary, out_path)

    assert out_path.exists()
    assert out_path.stat().st_size > 0
