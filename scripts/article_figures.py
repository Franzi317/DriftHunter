"""Figure generation for the DriftHunter article (`docs/article.md`).

This script does NOT modify engine code, gate logic, or any committed
report. It reads the already-computed event-study artifacts
(`data/events.parquet` from Phase 0, `data/study2_events_spy.parquet` from
Study 2) and renders the two article figures into `docs/figures/`.

Determinism: `compute_horizon_summary` is a pure function (frame in, frame
out) -- no I/O, no randomness beyond the seeded bootstrap in
`drifthunter.backtest.stats.bootstrap_ci` (seed=42, n_iter=10_000, fixed for
reproducibility). Calling it twice with the same inputs yields identical
output frames (including row order). `render_horizon_decay` is rendering
only and has no effect on the computed numbers.

Figure 2 (`horizon_decay.png`): grouped bar chart of mean excess return vs
SPY (30bp costs) by holding horizon, for both profiles (form4, sc13d), with
bootstrap CI whiskers. Phase 0 horizons (5/10/20/40) use the pre-registered
95% CI (alpha=0.05); Study 2 horizons (60/125/250) use the pre-registered 99%
CI (alpha=0.01) per `docs/study2-long-horizon-insider.md`.

Figure 1 (`event_time_excess.png`): event-time excess-return curve. For each
signal, prices are anchored at the LAST CLOSE STRICTLY BEFORE trigger_date
(no lookahead) and the cumulative SPY-adjusted excess return is tracked at a
range of trading-day offsets relative to that anchor. `compute_event_time_curve`
is a pure function (frame in, frame out); `render_event_time` is rendering
only.

Usage:
    uv run python scripts/article_figures.py
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless: must precede pyplot import

import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from drifthunter.backtest.stats import bootstrap_ci


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_ITER = 10_000
SEED = 42
HEADLINE_COST_BPS = 30

# Pre-registered CI levels: Phase 0 horizons (<=40) use 95%, Study 2 horizons
# (>=60) use 99% (docs/study2-long-horizon-insider.md).
PHASE0_ALPHA = 0.05
STUDY2_ALPHA = 0.01
HORIZON_ALPHA_THRESHOLD = 60

# Figure 1 price-fetch range. Mirrors `run_event_study`'s per-ticker range
# computation (drifthunter/backtest/event_study.py: LOOKBACK_CAL_DAYS=60,
# LOOKAHEAD_CAL_DAYS=90, end = max(triggers) + 90 + 2*max_horizon). We
# deliberately use max_horizon=250 (Study 2's max, not the small offset
# range used by `compute_event_time_curve`) so that the (ticker, start, end)
# cache keys computed here are IDENTICAL to the ones `study2_long_horizon.py`
# already populated -- this run becomes a pure cache-hit pass with no fresh
# fetches, as long as Study 2 has been run at least once.
EVENT_TIME_LOOKBACK_CAL_DAYS = 60
EVENT_TIME_LOOKAHEAD_CAL_DAYS = 90
EVENT_TIME_MAX_HORIZON = 250  # Study 2's max(HORIZONS); see docstring above.

# Colorblind-safe pair (Wong, 2011): blue / vermillion.
PROFILE_COLORS = {
    "form4": "#0072B2",
    "sc13d": "#D55E00",
}


# ---------------------------------------------------------------------------
# Pure compute
# ---------------------------------------------------------------------------

def _alpha_for_horizon(horizon: int) -> float:
    return STUDY2_ALPHA if horizon >= HORIZON_ALPHA_THRESHOLD else PHASE0_ALPHA


def compute_horizon_summary(events_p0: pd.DataFrame, events_s2: pd.DataFrame) -> pd.DataFrame:
    """Per (profile, horizon) mean excess return and bootstrap CI.

    Inputs are Phase 0 events (horizons 5/10/20/40) and Study 2 events
    (horizons 60/125/250), both in the standard event-study schema
    (profile, ticker, trigger_date, score, horizon, cost_bps, entry_date,
    exit_date, raw_return, excess_return, filter_reason). Both are filtered
    to `cost_bps == 30` and `filter_reason == ""` (completed events at the
    headline cost) before combining.

    The CI level depends on the horizon, not which input frame it came from:
    horizons <= 40 use the Phase 0 pre-registered 95% CI (alpha=0.05);
    horizons >= 60 use the Study 2 pre-registered 99% CI (alpha=0.01). This
    means the function does not need to know which frame a horizon
    originated from -- it works for whatever horizons are present in either
    input.

    Returns one row per (profile, horizon) present in the combined,
    filtered data, sorted by (profile, horizon) for deterministic output.

    Columns: profile, horizon, mean, ci_lo, ci_hi, ci_level.
    """
    def _filter(df: pd.DataFrame) -> pd.DataFrame:
        return df[(df["cost_bps"] == HEADLINE_COST_BPS) & (df["filter_reason"] == "")]

    combined = pd.concat([_filter(events_p0), _filter(events_s2)], ignore_index=True)

    rows: list[dict] = []
    groups = combined.groupby(["profile", "horizon"], sort=True)
    for (profile, horizon), group in groups:
        horizon = int(horizon)
        alpha = _alpha_for_horizon(horizon)
        ci = bootstrap_ci(
            group["excess_return"].to_numpy(dtype=float),
            n_iter=N_ITER,
            seed=SEED,
            alpha=alpha,
        )
        rows.append({
            "profile": profile,
            "horizon": horizon,
            "mean": ci.mean,
            "ci_lo": ci.lo,
            "ci_hi": ci.hi,
            "ci_level": 1.0 - alpha,
        })

    summary = pd.DataFrame(
        rows, columns=["profile", "horizon", "mean", "ci_lo", "ci_hi", "ci_level"]
    )
    summary = summary.sort_values(["profile", "horizon"]).reset_index(drop=True)
    return summary


# Default offset range for Figure 1: 5 trading days before the anchor through
# 60 trading days after.
DEFAULT_EVENT_TIME_OFFSETS = range(-5, 61)


def compute_event_time_curve(signals, price_frames: dict[str, pd.DataFrame],
                              spy_frame: pd.DataFrame,
                              offsets=DEFAULT_EVENT_TIME_OFFSETS) -> pd.DataFrame:
    """Per (profile, offset) mean cumulative SPY-adjusted excess return in
    event time, anchored at the last close STRICTLY BEFORE each signal's
    trigger_date.

    `signals` is a list of objects with `.profile`, `.ticker`,
    `.trigger_date` attributes (e.g. `drifthunter.scorer.models.Signal`).
    `price_frames` maps ticker -> daily frame (DatetimeIndex, columns
    open/close/volume -- the standard provider output, sorted, deduped).
    `spy_frame` is SPY's daily frame in the same schema, spanning at least
    the union of all anchor and offset dates used below.

    NO LOOKAHEAD: for each signal, `pre_pos` is the positional index of the
    last row in that ticker's frame with a date strictly before
    `trigger_date`. If no such row exists (the frame starts on or after
    trigger_date), or the ticker is missing from `price_frames` or has an
    empty frame, the signal is skipped entirely. The anchor close
    `c0 = close[pre_pos]` and anchor date `d0 = index[pre_pos]` define event
    time zero. If `d0` is not in `spy_frame`'s index, the signal is skipped
    entirely (the SPY anchor return cannot be computed for any offset).

    For each offset `k` in `offsets`, the target position is
    `p = pre_pos + k`. If `p` is out of bounds for the ticker's frame
    (`p < 0` or `p >= len(frame)`), this signal contributes NOTHING at offset
    `k` -- no fill, no carry-forward. Otherwise the ticker-frame date at `p`
    is looked up in `spy_frame`'s index; if it is missing there too, this
    signal contributes nothing at offset `k` (skipped, not approximated).
    Otherwise:

        cum_excess_k = (close[p] / c0 - 1) - (spy_close[date_p] / spy_close[d0] - 1)

    Offset 0 is the anchor row itself (`p == pre_pos`, `date_p == d0`), so
    `cum_excess_0 = 0 - 0 = 0` for every contributing signal by construction
    -- a sanity property of the output, not special-cased in this function.

    Weekend/holiday triggers: `pre_pos` is simply the last trading day before
    trigger_date, however many calendar days that is (e.g. a Monday
    trigger_date anchors to the prior Friday's close). The filing-day close
    then "collapses" into offset 1 (the next trading day after the anchor).
    These signals are kept as-is -- no special handling.

    Returns one row per (profile, offset) with at least one contributing
    signal, sorted by (profile, offset). Columns: profile, offset,
    mean_cum_excess (mean over contributing signals), n (contributor count).
    (profile, offset) combinations with zero contributors are omitted.

    Note on sample composition: `n` shrinks as |offset| grows, because
    signals whose price history doesn't extend far enough (shorter-history
    tickers, or signals near the start/end of the available price range)
    drop out of the contributor set at large offsets. The curve is therefore
    a cross-sectional average over a CHANGING SAMPLE at each offset, not a
    fixed cohort tracked through time -- article captions referencing this
    curve must disclose this.
    """
    offsets_list = list(offsets)

    sums: dict[tuple[str, int], float] = {}
    counts: dict[tuple[str, int], int] = {}

    for sig in signals:
        frame = price_frames.get(sig.ticker)
        if frame is None or frame.empty:
            continue

        trigger_ts = pd.Timestamp(sig.trigger_date)
        before = frame.index < trigger_ts
        n_before = int(before.sum())
        if n_before == 0:
            continue
        pre_pos = n_before - 1

        d0 = frame.index[pre_pos]
        if d0 not in spy_frame.index:
            continue

        c0 = float(frame["close"].iloc[pre_pos])
        spy_c0 = float(spy_frame.loc[d0, "close"])

        for k in offsets_list:
            p = pre_pos + k
            if p < 0 or p >= len(frame):
                continue

            date_p = frame.index[p]
            if date_p not in spy_frame.index:
                continue

            c_p = float(frame["close"].iloc[p])
            spy_c_p = float(spy_frame.loc[date_p, "close"])

            cum_excess = (c_p / c0 - 1.0) - (spy_c_p / spy_c0 - 1.0)

            key = (sig.profile, k)
            sums[key] = sums.get(key, 0.0) + cum_excess
            counts[key] = counts.get(key, 0) + 1

    rows = [
        {
            "profile": profile,
            "offset": offset,
            "mean_cum_excess": sums[(profile, offset)] / counts[(profile, offset)],
            "n": counts[(profile, offset)],
        }
        for (profile, offset) in sums
    ]

    curve = pd.DataFrame(rows, columns=["profile", "offset", "mean_cum_excess", "n"])
    curve = curve.sort_values(["profile", "offset"]).reset_index(drop=True)
    return curve


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _build_decay_axes(summary: pd.DataFrame) -> tuple[plt.Figure, plt.Axes]:
    """Build the Figure 2 (horizon decay) figure and axes.

    Grouped bar chart: mean excess return vs SPY (30bp costs) by holding
    horizon, one bar group per profile, with asymmetric bootstrap CI
    whiskers. Horizons are ordered ascending and treated as categorical
    (evenly spaced on the x-axis regardless of their numeric spacing, so
    5/10/20/40/60/125/250 don't visually compress the early horizons).

    A (profile, horizon) combination absent from `summary` is omitted
    entirely -- no bar, no whisker -- rather than rendered as a
    zero-height bar (which would be visually indistinguishable from a
    measured ~0% mean with a tight CI). X positions are still derived
    from the global sorted horizon list so bar groups stay aligned across
    profiles.
    """
    horizons = sorted(summary["horizon"].unique())
    profiles = [p for p in ("form4", "sc13d") if p in set(summary["profile"])]
    # Any profile not in the known pair still gets plotted (extra color).
    for p in sorted(summary["profile"].unique()):
        if p not in profiles:
            profiles.append(p)

    n_profiles = len(profiles)
    n_horizons = len(horizons)
    x = np.arange(n_horizons)
    bar_width = 0.8 / max(n_profiles, 1)
    horizon_to_x = dict(zip(horizons, x))

    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=160)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    for i, profile in enumerate(profiles):
        prof_rows = summary[summary["profile"] == profile].set_index("horizon")
        positions = []
        means = []
        err_lo = []
        err_hi = []
        for h in horizons:
            if h not in prof_rows.index:
                continue
            row = prof_rows.loc[h]
            mean = float(row["mean"])
            lo = float(row["ci_lo"])
            hi = float(row["ci_hi"])
            positions.append(horizon_to_x[h])
            means.append(mean)
            err_lo.append(max(mean - lo, 0.0))
            err_hi.append(max(hi - mean, 0.0))

        if not positions:
            continue

        offset = (i - (n_profiles - 1) / 2) * bar_width
        color = PROFILE_COLORS.get(profile, f"C{i}")
        ax.bar(
            np.array(positions) + offset,
            means,
            width=bar_width * 0.9,
            label=profile,
            color=color,
            yerr=[err_lo, err_hi],
            capsize=3,
            error_kw={"elinewidth": 1, "ecolor": "black"},
        )

    # Emphasized zero line.
    ax.axhline(0.0, color="black", linewidth=1.0, zorder=0)

    ax.set_xticks(x)
    ax.set_xticklabels([str(h) for h in horizons])
    ax.set_xlabel("Holding horizon (trading days)")
    ax.set_ylabel("Mean excess return vs SPY (30bp costs)")
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))

    ax.legend(frameon=False)

    ax.text(
        0.01, 0.02,
        "whiskers: 95% CI (≤40d), 99% CI (≥60d, pre-registered)",
        transform=ax.transAxes,
        fontsize=8,
        color="#555555",
        va="bottom",
        ha="left",
    )

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()
    return fig, ax


def render_horizon_decay(summary: pd.DataFrame, out_path: Path) -> None:
    """Render Figure 2 (horizon decay) to `out_path` as a PNG."""
    fig, _ax = _build_decay_axes(summary)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor="white")
    plt.close(fig)


# x-position (in trading-day offsets) of the earliest possible next-open
# entry: the filing-day close is offset 1, so the earliest open a
# next-open strategy can transact at is the open of offset 2 -- the dashed
# line sits at the midpoint (1.5) to visually separate "filing day" (<=1)
# from "earliest tradeable entry" (>=2).
NEXT_OPEN_ENTRY_X = 1.5


def _build_event_time_axes(curve: pd.DataFrame) -> tuple[plt.Figure, plt.Axes]:
    """Build the Figure 1 (event-time excess curve) figure and axes.

    One line per profile (form4, sc13d), x = offset (trading days relative
    to the last pre-filing close, offset 0), y = mean cumulative
    SPY-adjusted excess return. A dashed vertical line at x=1.5 marks the
    earliest possible next-open entry (offset 1 is the filing-day close;
    offset 2 is the first open a next-open strategy could transact at).
    Annotation label positions are derived from the curve's own values so
    they track the data rather than risk overlapping it.
    """
    profiles = [p for p in ("form4", "sc13d") if p in set(curve["profile"])]
    for p in sorted(curve["profile"].unique()):
        if p not in profiles:
            profiles.append(p)

    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=160)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    for i, profile in enumerate(profiles):
        prof_rows = curve[curve["profile"] == profile].sort_values("offset")
        if prof_rows.empty:
            continue
        color = PROFILE_COLORS.get(profile, f"C{i}")
        ax.plot(
            prof_rows["offset"], prof_rows["mean_cum_excess"],
            label=profile, color=color, linewidth=1.8,
        )

    # Zero line.
    ax.axhline(0.0, color="black", linewidth=1.0, zorder=0)

    # Earliest possible next-open entry.
    ax.axvline(NEXT_OPEN_ENTRY_X, color="#555555", linewidth=1.0, linestyle="--", zorder=0)

    # Reserve a headroom band above the data for annotation text, so labels
    # never overlap the plotted curves. Derived from the data's own y-range
    # (not a hardcoded magic number that could clip or collide).
    y_lo, y_hi = ax.get_ylim()
    y_span = y_hi - y_lo
    headroom = 0.12 * y_span
    new_y_hi = y_hi + headroom
    ax.set_ylim(y_lo, new_y_hi)
    label_y = y_hi + headroom / 2.0  # vertical center of the headroom band

    # "the pop": label in the headroom band, above the 0->2 step region.
    pop_window = curve[(curve["offset"] >= 0) & (curve["offset"] <= 2)]
    if not pop_window.empty:
        pop_x = float(pop_window["offset"].mean())
        ax.annotate(
            "the pop",
            xy=(pop_x, label_y),
            ha="center", va="center", fontsize=9, color="#333333",
        )

    # "what a next-open strategy gets": label in the headroom band, over the
    # region right of the entry line (right-aligned to the curve's right
    # edge).
    post_window = curve[curve["offset"] >= 2]
    if not post_window.empty:
        post_x = float(post_window["offset"].max())
        ax.annotate(
            "what a next-open strategy gets",
            xy=(post_x, label_y),
            ha="right", va="center", fontsize=9, color="#333333",
        )

    # Label for the vertical line itself, near the bottom of the axes (out
    # of the way of both the curves and the headroom-band labels above).
    ax.annotate(
        "earliest possible next-open entry",
        xy=(NEXT_OPEN_ENTRY_X, y_lo),
        xytext=(NEXT_OPEN_ENTRY_X + 0.5, y_lo + 0.02 * y_span),
        ha="left", va="bottom", fontsize=8, color="#555555",
    )

    ax.set_xlabel("Trading days from last pre-filing close")
    ax.set_ylabel("Mean cumulative excess return vs SPY")
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))

    # Pinned to the pre-anchor region (offsets < 0 are ~flat at zero by
    # construction), away from the headroom-band annotations at the top and
    # the post-entry curves to the right.
    ax.legend(loc="center left", frameon=False)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()
    return fig, ax


def render_event_time(curve: pd.DataFrame, out_path: Path) -> None:
    """Render Figure 1 (event-time excess curve) to `out_path` as a PNG."""
    fig, _ax = _build_event_time_axes(curve)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    events_p0 = pd.read_parquet(REPO_ROOT / "data" / "events.parquet")
    events_s2 = pd.read_parquet(REPO_ROOT / "data" / "study2_events_spy.parquet")

    summary = compute_horizon_summary(events_p0, events_s2)

    decay_path = REPO_ROOT / "docs" / "figures" / "horizon_decay.png"
    render_horizon_decay(summary, decay_path)
    print(f"Wrote {decay_path}")

    # -----------------------------------------------------------------
    # Figure 1: event-time excess curve
    # -----------------------------------------------------------------
    print(
        "note: cache-hit pass assumes Study 2 ran with max horizon 250",
        file=sys.stderr,
    )

    from drifthunter.config import load_config

    # Reuse `study2_long_horizon`'s signal loading and provider stack
    # exactly -- same Signal reconstruction (so the same per-ticker
    # grouping) and same SpyIwmFreeRoutingProvider (SPY routed to the free
    # provider; cached reads need no API key).
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from study2_long_horizon import load_signals, make_provider  # noqa: E402

    cfg = load_config(REPO_ROOT / "config.yaml")
    signals = load_signals(cfg)
    provider = make_provider(cfg)

    # Per-ticker price ranges: SAME computation as run_event_study /
    # study2_long_horizon, using Study 2's max_horizon (250) -- see
    # EVENT_TIME_MAX_HORIZON docstring above for why. This makes the fetch
    # below a cache-hit pass against the Study 2 cache.
    by_ticker: dict[str, list] = {}
    for sig in signals:
        by_ticker.setdefault(sig.ticker, []).append(sig)

    price_frames: dict[str, pd.DataFrame] = {}
    ticker_ranges: dict[str, tuple] = {}
    for ticker, sigs in by_ticker.items():
        triggers = [s.trigger_date for s in sigs]
        start = min(triggers) - timedelta(days=EVENT_TIME_LOOKBACK_CAL_DAYS)
        end = max(triggers) + timedelta(
            days=EVENT_TIME_LOOKAHEAD_CAL_DAYS + 2 * EVENT_TIME_MAX_HORIZON
        )
        ticker_ranges[ticker] = (start, end)
        price_frames[ticker] = provider.daily(ticker, start, end)

    global_start = min(s for s, _ in ticker_ranges.values())
    global_end = max(e for _, e in ticker_ranges.values())
    spy_frame = provider.daily("SPY", global_start, global_end)

    curve = compute_event_time_curve(signals, price_frames, spy_frame)

    event_time_path = REPO_ROOT / "docs" / "figures" / "event_time_excess.png"
    render_event_time(curve, event_time_path)
    print(f"Wrote {event_time_path}")

    # Report sample-size (n) at a few representative offsets per profile, for
    # the article caption (offset-0 and offset-60 n are required there).
    report_offsets = [0, 1, 2, 10, 30, 60]
    curve_by_key = curve.set_index(["profile", "offset"])["n"]
    for profile in sorted(curve["profile"].unique()):
        n_values = {
            k: int(curve_by_key.get((profile, k), 0)) for k in report_offsets
        }
        print(f"n by offset for profile={profile}: {n_values}")


if __name__ == "__main__":
    main()
