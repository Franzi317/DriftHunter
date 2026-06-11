import pandas as pd
import pytest

from drifthunter.config import PortfolioConfig
from drifthunter.backtest.portfolio import simulate

CFG = PortfolioConfig(bankroll=5000.0, position_size=1000.0,
                      max_positions=2, max_entries_per_day=1)


def event(ticker, entry, exit_, ret, score=2.0):
    return {
        "profile": "form4", "ticker": ticker, "score": score,
        "trigger_date": pd.Timestamp(entry) - pd.Timedelta(days=1),
        "entry_date": pd.Timestamp(entry), "exit_date": pd.Timestamp(exit_),
        "raw_return": ret, "excess_return": ret, "filter_reason": "",
        "horizon": 2, "cost_bps": 30,
    }


def test_pnl_of_single_trade():
    df = pd.DataFrame([event("A", "2024-03-04", "2024-03-06", 0.10)])
    result = simulate(df, CFG)
    assert result.final_equity == pytest.approx(5000.0 + 1000.0 * 0.10)
    assert result.trades_taken == 1
    assert result.skipped_full_book == 0


def test_max_positions_enforced():
    df = pd.DataFrame([
        event("A", "2024-03-04", "2024-03-20", 0.0),
        event("B", "2024-03-05", "2024-03-20", 0.0),
        event("C", "2024-03-06", "2024-03-20", 0.0),  # book full -> skipped
    ])
    result = simulate(df, CFG)
    assert result.trades_taken == 2
    assert result.skipped_full_book == 1


def test_max_entries_per_day_enforced():
    df = pd.DataFrame([
        event("A", "2024-03-04", "2024-03-20", 0.0, score=3.0),
        event("B", "2024-03-04", "2024-03-20", 0.0, score=1.0),  # same day, cap 1
    ])
    result = simulate(df, CFG)
    assert result.trades_taken == 1
    assert result.skipped_entry_cap == 1


def test_higher_score_wins_same_day():
    df = pd.DataFrame([
        event("LOW", "2024-03-04", "2024-03-20", 0.0, score=1.0),
        event("HIGH", "2024-03-04", "2024-03-20", 0.0, score=9.0),
    ])
    result = simulate(df, CFG)
    assert result.tickers_traded == ["HIGH"]


def test_max_drawdown_on_realized_curve():
    df = pd.DataFrame([
        event("A", "2024-03-04", "2024-03-06", -0.50),
        event("B", "2024-03-07", "2024-03-11", 0.10),
    ])
    result = simulate(df, CFG)
    # After A: equity 4500 (dd = 500/5000 = 10%). B adds +100.
    assert result.max_drawdown == pytest.approx(0.10)
    assert result.final_equity == pytest.approx(4600.0)
