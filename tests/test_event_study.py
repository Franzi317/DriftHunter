from datetime import date

import pandas as pd
import pytest

from drifthunter.config import TradabilityConfig
from drifthunter.backtest.event_study import run_event_study
from drifthunter.scorer.models import Signal

TRAD = TradabilityConfig(min_price=2.0, min_avg_dollar_volume=1_000_000,
                         adv_window=3, allowed_exchanges=["NYSE", "Nasdaq"])


def prices(opens, start="2024-03-01", volume=500_000.0):
    idx = pd.bdate_range(start, periods=len(opens))
    return pd.DataFrame(
        {"open": opens, "close": opens, "volume": volume}, index=idx
    ).rename_axis("date")


class DictProvider:
    def __init__(self, frames):
        self.frames = frames
        self.calls = []

    def daily(self, ticker, start, end):
        self.calls.append(ticker)
        return self.frames.get(ticker, pd.DataFrame())


def test_entry_exit_and_excess_return():
    # Ticker rises 10.0 -> 11.0 between entry (Mar 4 open) and exit (Mar 6 open, horizon 2).
    # SPY flat. Cost 30bp. Expected excess = 0.10 - 0.003 - 0.0 = 0.097
    frames = {
        "EXM": prices([9.0, 10.0, 10.5, 11.0, 11.2], volume=500_000.0),
        "SPY": prices([400.0] * 5, volume=10_000_000.0),
    }
    sig = Signal(profile="form4", ticker="EXM", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[2], cost_bps_list=[30], benchmark="SPY")
    row = result[(result.horizon == 2) & (result.cost_bps == 30)].iloc[0]
    assert row["entry_date"] == pd.Timestamp("2024-03-04")
    assert row["excess_return"] == pytest.approx(0.097, abs=1e-9)
    assert row["filter_reason"] == ""


def test_low_price_filtered():
    frames = {
        "PNY": prices([1.5, 1.6, 1.7, 1.8, 1.9]),
        "SPY": prices([400.0] * 5),
    }
    sig = Signal(profile="form4", ticker="PNY", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[2], cost_bps_list=[30], benchmark="SPY")
    assert result.iloc[0]["filter_reason"] == "min_price"


def test_low_dollar_volume_filtered():
    frames = {
        "THIN": prices([10.0] * 5, volume=10_000.0),  # $100k/day << $1M
        "SPY": prices([400.0] * 5),
    }
    sig = Signal(profile="form4", ticker="THIN", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[2], cost_bps_list=[30], benchmark="SPY")
    assert result.iloc[0]["filter_reason"] == "adv"


def test_missing_prices_marked_uncovered():
    frames = {"SPY": prices([400.0] * 5)}
    sig = Signal(profile="sc13d", ticker="GONE", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[2], cost_bps_list=[30], benchmark="SPY")
    assert result.iloc[0]["filter_reason"] == "no_prices"


def test_insufficient_future_rows_skipped():
    frames = {
        "EXM": prices([10.0, 10.0, 10.0]),  # horizon 40 cannot complete
        "SPY": prices([400.0] * 3),
    }
    sig = Signal(profile="form4", ticker="EXM", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[40], cost_bps_list=[30], benchmark="SPY")
    assert result.iloc[0]["filter_reason"] == "insufficient_history"


def test_one_fetch_per_ticker_plus_benchmark():
    frames = {
        "EXM": prices([10.0] * 80),
        "SPY": prices([400.0] * 80, volume=1e7),
    }
    provider = DictProvider(frames)
    sigs = [
        Signal(profile="form4", ticker="EXM", trigger_date=date(2024, 3, 1), score=2.0, detail=""),
        Signal(profile="form4", ticker="EXM", trigger_date=date(2024, 3, 8), score=2.0, detail=""),
        Signal(profile="form4", ticker="EXM", trigger_date=date(2024, 4, 2), score=2.0, detail=""),
    ]
    run_event_study(sigs, provider, TRAD, horizons=[2], cost_bps_list=[30], benchmark="SPY")
    assert provider.calls.count("EXM") == 1
    assert provider.calls.count("SPY") == 1
