"""Bootstrap confidence intervals and yearly performance breakdowns.

`bootstrap_ci` resamples `values` with replacement `n_iter` times to build a
sampling distribution of the mean, then reports the central `1 - alpha`
interval. Resampling is seeded for reproducibility.

Memory note: the resample matrix is `n_iter x len(values)` float64. For the
defaults likely used here (n_iter=10000, ~2000 events) that's ~160MB, which
is acceptable for a one-shot backtest report. If `values` ever grows much
larger, this could be chunked (accumulating per-chunk means) to bound peak
memory.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BootstrapCI:
    mean: float
    lo: float
    hi: float
    n: int


def bootstrap_ci(values, n_iter: int, seed: int, alpha: float = 0.05) -> BootstrapCI:
    arr = np.asarray(values, dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        raise ValueError("bootstrap_ci requires at least one non-NaN value")
    rng = np.random.default_rng(seed)
    samples = rng.choice(arr, size=(n_iter, arr.size), replace=True)
    means = samples.mean(axis=1)
    return BootstrapCI(
        mean=float(arr.mean()),
        lo=float(np.quantile(means, alpha / 2)),
        hi=float(np.quantile(means, 1 - alpha / 2)),
        n=int(arr.size),
    )


def yearly_means(df: pd.DataFrame) -> dict[int, float]:
    """Mean excess_return grouped by trigger_date year.

    `trigger_date` may be `datetime64` (synthetic test frames) or
    object-dtype `date` values (run_event_study output, which carries
    Signal.trigger_date through unchanged); pd.to_datetime normalizes
    either to a `.dt`-capable series.
    """
    grouped = df.groupby(pd.to_datetime(df["trigger_date"]).dt.year)["excess_return"].mean()
    return {int(y): float(v) for y, v in grouped.items()}
