from datetime import date

import pandas as pd
import pytest

from drifthunter.prices.provider import CachingProvider, coverage_report


class FakeProvider:
    def __init__(self, available: dict[str, pd.DataFrame]):
        self.available = available
        self.calls = 0

    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        self.calls += 1
        return self.available.get(ticker, pd.DataFrame())


def make_prices(n=30, start="2024-01-02"):
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame(
        {"open": 10.0, "close": 10.0, "volume": 200_000.0}, index=idx
    ).rename_axis("date")


def test_cache_avoids_second_fetch(tmp_path):
    inner = FakeProvider({"EXM": make_prices()})
    p = CachingProvider(inner, tmp_path)
    a = p.daily("EXM", date(2024, 1, 2), date(2024, 2, 9))
    b = p.daily("EXM", date(2024, 1, 2), date(2024, 2, 9))
    assert inner.calls == 1
    pd.testing.assert_frame_equal(a, b)


def test_missing_ticker_returns_empty(tmp_path):
    p = CachingProvider(FakeProvider({}), tmp_path)
    assert p.daily("GONE", date(2024, 1, 2), date(2024, 2, 9)).empty


def test_coverage_report(tmp_path):
    p = CachingProvider(FakeProvider({"EXM": make_prices()}), tmp_path)
    cov = coverage_report(["EXM", "GONE", "EXM"], p, date(2024, 1, 2), date(2024, 2, 9))
    assert cov.total == 2          # deduped
    assert cov.covered == 1
    assert cov.rate == pytest.approx(0.5)
    assert cov.missing == ["GONE"]


def test_slash_ticker_does_not_crash_cache(tmp_path):
    inner = FakeProvider({"BF/B": make_prices()})
    p = CachingProvider(inner, tmp_path)
    df = p.daily("BF/B", date(2024, 1, 2), date(2024, 2, 9))
    assert not df.empty
    assert inner.calls == 1
    # second call served from cache
    p.daily("BF/B", date(2024, 1, 2), date(2024, 2, 9))
    assert inner.calls == 1
    # cache file written inside cache_dir, not a subdirectory
    files = list(tmp_path.glob("*.parquet"))
    assert len(files) == 1
