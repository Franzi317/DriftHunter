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
    # Net of 30bp round-trip cost: 1000 * (0.10 - 0.003) = 97.0
    assert result.final_equity == pytest.approx(5000.0 + 1000.0 * (0.10 - 0.003))
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
    # Net of 30bp round-trip cost on each leg.
    # A: proceeds = 1000 * (1 + (-0.50 - 0.003)) = 497.0 -> cash 4000+497=4497
    #    dd = (5000-4497)/5000 = 503/5000 = 0.1006
    # B: proceeds = 1000 * (1 + (0.10 - 0.003)) = 1097.0 -> cash 3497+1097=4594.0
    #    equity 4594 < peak 5000, dd stays at 0.1006 (max).
    assert result.max_drawdown == pytest.approx(0.1006)
    assert result.final_equity == pytest.approx(4594.0)


def test_all_or_nothing_sizing_skips_when_cash_insufficient():
    cfg = PortfolioConfig(bankroll=2500.0, position_size=1000.0,
                          max_positions=10, max_entries_per_day=10)
    df = pd.DataFrame([
        event("A", "2024-03-04", "2024-03-20", 0.0, score=3.0),
        event("B", "2024-03-04", "2024-03-20", 0.0, score=2.0),
        event("C", "2024-03-04", "2024-03-20", 0.0, score=1.0),
    ])
    result = simulate(df, cfg)
    # 2500 - 1000 (A) - 1000 (B) = 500 cash left; 500 < 1000 -> C skipped.
    assert result.trades_taken == 2
    assert result.skipped_no_cash == 1
