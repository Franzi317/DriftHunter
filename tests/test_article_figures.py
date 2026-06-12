"""Fixture-level tests for scripts/article_figures.py (Figure 1: event-time
excess curve; Figure 2: horizon decay).

No network, no provider, no real data: builds tiny synthetic event-study
frames matching `run_event_study`'s RESULT_COLUMNS schema and feeds them
straight to the pure function `compute_horizon_summary`, and tiny synthetic
price frames (matching the standard provider's open/close/volume schema) fed
to `compute_event_time_curve`. The render smoke tests only check that
`render_horizon_decay` / `render_event_time` produce nonempty PNG files
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

from article_figures import (  # noqa: E402
    _build_decay_axes,
    _build_event_time_axes,
    compute_event_time_curve,
    compute_horizon_summary,
    render_event_time,
    render_horizon_decay,
)


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


def test_sparse_summary_omits_absent_combo_bars(tmp_path):
    """A (profile, horizon) combo absent from the summary must not render a
    zero-height bar -- it should be omitted entirely. Build a sparse summary
    with form4 at horizons [5, 10] and sc13d at horizon [5] only (3 combos
    total, not the full 2x2=4), and assert the rendered axes has exactly 3
    bars."""
    events_p0 = pd.concat([
        _make_events("form4", [5, 10], n=40, excess_mean=0.001, seed=1),
        _make_events("sc13d", [5], n=40, excess_mean=-0.01, seed=2),
    ], ignore_index=True)
    events_s2 = events_p0.iloc[0:0]  # empty frame, correct schema

    summary = compute_horizon_summary(events_p0, events_s2)

    assert len(summary) == 3
    assert set(zip(summary["profile"], summary["horizon"])) == {
        ("form4", 5), ("form4", 10), ("sc13d", 5),
    }

    fig, ax = _build_decay_axes(summary)
    assert len(ax.patches) == 3

    out_path = tmp_path / "sparse.png"
    render_horizon_decay(summary, out_path)

    assert out_path.exists()
    assert out_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# Figure 1: event-time excess curve
# ---------------------------------------------------------------------------

from drifthunter.scorer.models import Signal  # noqa: E402


def _signal(profile: str, ticker: str, trigger_date) -> Signal:
    return Signal(
        profile=profile,
        ticker=ticker,
        trigger_date=pd.Timestamp(trigger_date).date(),
        score=2.0,
        detail="",
    )


def _step_frame(n_rows: int = 80, step_pos: int = 6, step2_pos: int = 7,
                 pre_close: float = 10.0, step1_close: float = 10.5,
                 step2_close: float = 11.0) -> pd.DataFrame:
    """A bdate-indexed close series flat at `pre_close` through `step_pos - 1`,
    `step1_close` at `step_pos`, `step2_close` at `step2_pos` and after."""
    idx = pd.bdate_range("2024-01-01", periods=n_rows)
    closes = np.full(n_rows, pre_close, dtype=float)
    closes[step_pos:step2_pos] = step1_close
    closes[step2_pos:] = step2_close
    return pd.DataFrame(
        {"open": closes, "close": closes, "volume": 1_000_000.0}, index=idx
    )


def _flat_spy_frame(n_rows: int = 80, level: float = 400.0) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=n_rows)
    vals = np.full(n_rows, level, dtype=float)
    return pd.DataFrame(
        {"open": vals, "close": vals, "volume": 1_000_000.0}, index=idx
    )


def test_event_time_basic_two_signals_hand_computed():
    """Two form4 signals on the same single-ticker timeline; trigger_date =
    index[6] -> pre_pos = 5 (index[5]). Close is flat 10.0 through pre_pos,
    steps to 10.5 at offset 1 (index[6], the filing day), 11.0 at offset 2
    (index[7]), then flat 11.0. SPY flat 400 (no SPY adjustment).

    Hand-computed: offset 0 -> 0.0, offset 1 -> 0.05, offset 2 -> 0.10,
    offset 10 -> 0.10 (flat after the step)."""
    frame = _step_frame()
    spy = _flat_spy_frame()
    trigger_date = frame.index[6]

    signals = [
        _signal("form4", "AAA", trigger_date),
        _signal("form4", "AAA", trigger_date),
    ]
    price_frames = {"AAA": frame}

    curve = compute_event_time_curve(signals, price_frames, spy)

    by_offset = curve.set_index(["profile", "offset"])

    assert by_offset.loc[("form4", 0), "mean_cum_excess"] == pytest.approx(0.0, abs=1e-12)
    assert by_offset.loc[("form4", 1), "mean_cum_excess"] == pytest.approx(0.05, abs=1e-12)
    assert by_offset.loc[("form4", 2), "mean_cum_excess"] == pytest.approx(0.10, abs=1e-12)
    assert by_offset.loc[("form4", 10), "mean_cum_excess"] == pytest.approx(0.10, abs=1e-12)

    # Both signals contribute identically at every in-range offset.
    assert by_offset.loc[("form4", 0), "n"] == 2
    assert by_offset.loc[("form4", 2), "n"] == 2


def test_event_time_offset_zero_is_always_zero():
    """Sanity property: offset 0 is the anchor row itself, so
    cum_excess_0 = 0 - 0 = 0 by construction for every signal, regardless of
    profile or price path."""
    frame = _step_frame()
    spy = _flat_spy_frame()
    trigger_date = frame.index[6]

    signals = [
        _signal("form4", "AAA", trigger_date),
        _signal("sc13d", "AAA", trigger_date),
    ]
    price_frames = {"AAA": frame}

    curve = compute_event_time_curve(signals, price_frames, spy)
    zeros = curve[curve["offset"] == 0]
    assert len(zeros) == 2
    for _, row in zeros.iterrows():
        assert row["mean_cum_excess"] == pytest.approx(0.0, abs=1e-12)


def test_event_time_spy_adjustment():
    """Same ticker price steps as the basic test, but SPY also rises 1% by
    offset 2. The excess at offset 2 must be reduced by that 1%:
    0.10 - 0.01 = 0.09."""
    frame = _step_frame()
    spy = _flat_spy_frame()

    trigger_date = frame.index[6]
    pre_pos = 5  # index[5]

    # SPY rises 1% at offset 2 (index[7]) and stays there.
    spy_closes = spy["close"].to_numpy().copy()
    spy_closes[pre_pos + 2:] = 400.0 * 1.01
    spy = pd.DataFrame(
        {"open": spy_closes, "close": spy_closes, "volume": spy["volume"]},
        index=spy.index,
    )

    signals = [_signal("form4", "AAA", trigger_date)]
    price_frames = {"AAA": frame}

    curve = compute_event_time_curve(signals, price_frames, spy)
    by_offset = curve.set_index(["profile", "offset"])

    assert by_offset.loc[("form4", 2), "mean_cum_excess"] == pytest.approx(0.10 - 0.01, abs=1e-12)


def test_event_time_truncation_drops_contributors():
    """A signal whose frame ends 3 rows after pre_pos contributes to offsets
    <= 3 only. With a second full-length signal on a different ticker, n
    drops from 2 to 1 (and the mean changes accordingly) for offsets > 3."""
    full_frame = _step_frame()
    spy = _flat_spy_frame()
    trigger_date = full_frame.index[6]
    pre_pos = 5

    # Truncated frame: same prices, but only 3 rows past pre_pos (offsets 0..3).
    short_frame = full_frame.iloc[: pre_pos + 4].copy()

    signals = [
        _signal("form4", "AAA", trigger_date),  # full-length
        _signal("form4", "BBB", trigger_date),  # truncated
    ]
    price_frames = {"AAA": full_frame, "BBB": short_frame}

    curve = compute_event_time_curve(signals, price_frames, spy)
    by_offset = curve.set_index(["profile", "offset"])

    # Both contribute through offset 3.
    assert by_offset.loc[("form4", 3), "n"] == 2
    # Only AAA contributes beyond offset 3.
    assert by_offset.loc[("form4", 10), "n"] == 1

    # AAA's value at offset 10 is 0.10 (flat after the step); the mean with
    # n=1 must equal AAA's own contribution exactly.
    assert by_offset.loc[("form4", 10), "mean_cum_excess"] == pytest.approx(0.10, abs=1e-12)

    # At offset 3 both AAA and BBB contribute 0.10 (flat after the step),
    # so the mean is still 0.10 with n=2.
    assert by_offset.loc[("form4", 3), "mean_cum_excess"] == pytest.approx(0.10, abs=1e-12)
    assert by_offset.loc[("form4", 3), "n"] == 2


def test_event_time_no_lookahead_anchor_is_prior_trading_day():
    """A trigger_date that falls ON a Monday (a date present in the index)
    must anchor to the PRECEDING Friday's row -- the last row strictly
    before trigger_date -- never the Monday row itself."""
    frame = _step_frame()
    spy = _flat_spy_frame()

    # index[5] = 2024-01-08 (a Monday); index[4] = 2024-01-05 (the prior Friday).
    monday = frame.index[5]
    friday = frame.index[4]
    assert monday.day_name() == "Monday"
    assert friday.day_name() == "Friday"

    signals = [_signal("form4", "AAA", monday)]
    price_frames = {"AAA": frame}

    curve = compute_event_time_curve(signals, price_frames, spy, offsets=range(0, 1))

    # offset 0 must be 0 regardless (sanity property holds even here)...
    assert curve.set_index(["profile", "offset"]).loc[("form4", 0), "mean_cum_excess"] \
        == pytest.approx(0.0, abs=1e-12)

    # ...but to actually confirm the anchor landed on Friday (index[4], close
    # 10.0) and not Monday (index[5], close 10.5 in `_step_frame`'s default
    # step_pos=6 -- wait, step_pos=6 means index[6] steps, so index[5] is
    # still 10.0 too). Use a frame where the Monday row itself differs from
    # Friday's to disambiguate: step at index[5] (the Monday).
    frame2 = _step_frame(step_pos=5, step2_pos=6)  # steps to 10.5 AT the Monday row
    price_frames2 = {"AAA": frame2}

    curve2 = compute_event_time_curve(
        signals, price_frames2, spy, offsets=range(0, 2)
    )
    by_offset2 = curve2.set_index(["profile", "offset"])

    # pre_pos must be index[4] (Friday, close 10.0) -- the anchor c0 = 10.0.
    # offset 1 = index[5] (Monday, close 10.5 after the step) ->
    # cum_excess_1 = 10.5/10.0 - 1 = 0.05.
    assert by_offset2.loc[("form4", 0), "mean_cum_excess"] == pytest.approx(0.0, abs=1e-12)
    assert by_offset2.loc[("form4", 1), "mean_cum_excess"] == pytest.approx(0.05, abs=1e-12)


def test_event_time_skips_signal_with_no_pre_trigger_row():
    """A signal whose trigger_date is on or before the very first row of its
    ticker's frame has no row strictly before trigger_date and must be
    skipped entirely (no contribution at any offset)."""
    frame = _step_frame()
    spy = _flat_spy_frame()

    signals = [_signal("form4", "AAA", frame.index[0])]
    price_frames = {"AAA": frame}

    curve = compute_event_time_curve(signals, price_frames, spy)
    assert curve.empty


def test_event_time_skips_signal_with_missing_ticker():
    """A signal referencing a ticker absent from `price_frames` (or with an
    empty frame) is skipped entirely."""
    frame = _step_frame()
    spy = _flat_spy_frame()
    trigger_date = frame.index[6]

    signals = [
        _signal("form4", "AAA", trigger_date),
        _signal("form4", "ZZZ", trigger_date),  # missing
        _signal("form4", "EMPTY", trigger_date),  # empty frame
    ]
    price_frames = {"AAA": frame, "EMPTY": frame.iloc[0:0]}

    curve = compute_event_time_curve(signals, price_frames, spy)
    by_offset = curve.set_index(["profile", "offset"])

    # Only AAA contributes.
    assert by_offset.loc[("form4", 0), "n"] == 1


def test_event_time_skips_signal_when_anchor_date_missing_from_spy():
    """If the anchor date d0 is not in SPY's index, the whole signal is
    skipped (not just the offsets touching that date)."""
    frame = _step_frame()
    trigger_date = frame.index[6]
    pre_pos = 5
    d0 = frame.index[pre_pos]

    # SPY frame missing the anchor date entirely.
    spy_full = _flat_spy_frame()
    spy = spy_full.drop(index=d0)

    signals = [_signal("form4", "AAA", trigger_date)]
    price_frames = {"AAA": frame}

    curve = compute_event_time_curve(signals, price_frames, spy)
    assert curve.empty


def test_event_time_offsets_outside_range_omitted_when_n_zero():
    """Offsets where pre_pos + k is out of bounds for ALL signals (n == 0)
    must not appear as rows at all."""
    frame = _step_frame(n_rows=10)  # only 10 rows total
    spy = _flat_spy_frame(n_rows=10)
    trigger_date = frame.index[6]  # pre_pos = 5; only 4 rows remain (5..9)

    signals = [_signal("form4", "AAA", trigger_date)]
    price_frames = {"AAA": frame}

    curve = compute_event_time_curve(signals, price_frames, spy, offsets=range(-5, 61))
    offsets_present = set(curve["offset"])

    # In-range offsets: -5 (pre_pos-5 = 0, valid) .. 4 (pre_pos+4=9, valid).
    assert -5 in offsets_present
    assert 4 in offsets_present
    # Out-of-range offsets must be absent entirely.
    assert 5 not in offsets_present
    assert 60 not in offsets_present
    assert -6 not in offsets_present  # not even in the requested range, but double-check


def test_event_time_curve_columns_and_sort_order():
    frame = _step_frame()
    spy = _flat_spy_frame()
    trigger_date = frame.index[6]

    signals = [
        _signal("sc13d", "AAA", trigger_date),
        _signal("form4", "AAA", trigger_date),
    ]
    price_frames = {"AAA": frame}

    curve = compute_event_time_curve(signals, price_frames, spy)

    assert list(curve.columns) == ["profile", "offset", "mean_cum_excess", "n"]

    sorted_curve = curve.sort_values(["profile", "offset"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(curve.reset_index(drop=True), sorted_curve)

    # form4 sorts before sc13d.
    assert list(curve["profile"].unique()) == ["form4", "sc13d"]


def test_render_event_time_creates_nonempty_png_and_two_lines(tmp_path):
    frame = _step_frame()
    spy = _flat_spy_frame()
    trigger_date = frame.index[6]

    signals = [
        _signal("form4", "AAA", trigger_date),
        _signal("sc13d", "AAA", trigger_date),
    ]
    price_frames = {"AAA": frame}

    curve = compute_event_time_curve(signals, price_frames, spy)

    fig, ax = _build_event_time_axes(curve)
    labels = [line.get_label() for line in ax.get_lines()]
    assert "form4" in labels
    assert "sc13d" in labels

    out_path = tmp_path / "event_time.png"
    render_event_time(curve, out_path)

    assert out_path.exists()
    assert out_path.stat().st_size > 0
