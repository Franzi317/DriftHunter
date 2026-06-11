from datetime import date

import pandas as pd

from drifthunter.prices.sharadar import SharadarProvider


def test_normalizes_sep_table(monkeypatch):
    fake = pd.DataFrame({
        "ticker": ["EXM", "EXM"],
        "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "open": [10.0, 10.5],
        "close": [10.4, 10.2],
        "volume": [1_000_000.0, 900_000.0],
    })
    provider = SharadarProvider(api_key="test")
    monkeypatch.setattr(provider, "_get_table", lambda ticker, start, end: fake)
    df = provider.daily("EXM", date(2024, 1, 1), date(2024, 1, 31))
    assert list(df.columns) == ["open", "close", "volume"]
    assert df.index.name == "date"
    assert len(df) == 2


def test_empty_result_for_unknown_ticker(monkeypatch):
    provider = SharadarProvider(api_key="test")
    monkeypatch.setattr(provider, "_get_table",
                        lambda ticker, start, end: pd.DataFrame())
    assert provider.daily("GONE", date(2024, 1, 1), date(2024, 1, 31)).empty
