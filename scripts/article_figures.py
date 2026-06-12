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

Figure 1 (`event_time_excess.png`) is added by a later task in the article
package plan; `main()` below is a scaffold that will be extended to produce
it.

Usage:
    uv run python scripts/article_figures.py
"""
from __future__ import annotations

import sys
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    events_p0 = pd.read_parquet(REPO_ROOT / "data" / "events.parquet")
    events_s2 = pd.read_parquet(REPO_ROOT / "data" / "study2_events_spy.parquet")

    summary = compute_horizon_summary(events_p0, events_s2)

    out_path = REPO_ROOT / "docs" / "figures" / "horizon_decay.png"
    render_horizon_decay(summary, out_path)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
