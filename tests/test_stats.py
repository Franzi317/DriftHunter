import numpy as np
import pandas as pd
import pytest

from drifthunter.backtest.stats import bootstrap_ci, yearly_means


def test_bootstrap_ci_deterministic_and_sane():
    rng = np.random.default_rng(0)
    values = rng.normal(0.01, 0.05, size=500)
    a = bootstrap_ci(values, n_iter=2000, seed=42)
    b = bootstrap_ci(values, n_iter=2000, seed=42)
    assert a == b                       # determinism
    assert a.lo < a.mean < a.hi
    assert a.mean == pytest.approx(values.mean(), abs=1e-12)


def test_bootstrap_ci_excludes_zero_for_strong_effect():
    values = np.full(300, 0.02) + np.random.default_rng(1).normal(0, 0.001, 300)
    ci = bootstrap_ci(values, n_iter=2000, seed=42)
    assert ci.lo > 0


def test_bootstrap_ci_empty_raises():
    with pytest.raises(ValueError, match="at least one"):
        bootstrap_ci([float("nan")], n_iter=100, seed=1)


def test_yearly_means():
    df = pd.DataFrame({
        "trigger_date": pd.to_datetime(["2021-05-01", "2021-06-01", "2022-05-01"]),
        "excess_return": [0.10, -0.02, 0.05],
    })
    by_year = yearly_means(df)
    assert by_year[2021] == pytest.approx(0.04)
    assert by_year[2022] == pytest.approx(0.05)
