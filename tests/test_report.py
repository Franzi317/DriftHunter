import numpy as np
import pandas as pd

from drifthunter.config import GateConfig, PortfolioConfig
from drifthunter.backtest.report import evaluate_gate, render_markdown

GATE = GateConfig(form4_min_events=10, sc13d_min_events=5, min_years_positive=3,
                  total_years=5, max_drawdown=0.25, min_coverage=0.80)
PORT = PortfolioConfig(bankroll=5000.0, position_size=1000.0,
                       max_positions=5, max_entries_per_day=2)


def make_events(profile, n, mean_ret, horizon=10, cost=30, start_year=2021):
    rng = np.random.default_rng(7)
    dates = pd.to_datetime([
        f"{start_year + (i % 5)}-{(i % 12) + 1:02d}-15" for i in range(n)
    ])
    entry = dates + pd.Timedelta(days=1)
    return pd.DataFrame({
        "profile": profile, "ticker": [f"T{i}" for i in range(n)],
        "score": 2.0, "trigger_date": dates, "horizon": horizon, "cost_bps": cost,
        "entry_date": entry, "exit_date": entry + pd.Timedelta(days=horizon),
        "raw_return": rng.normal(mean_ret, 0.01, n),
        "excess_return": rng.normal(mean_ret, 0.01, n),
        "filter_reason": "",
    })


def test_strong_profile_goes():
    events = make_events("form4", 200, mean_ret=0.03)
    verdict = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                            gate=GATE, port=PORT, headline_cost_bps=30,
                            horizons=[10], seed=42, spy_annual_return=0.0)
    assert verdict.decision == "GO"


def test_zero_edge_profile_kills():
    events = make_events("form4", 200, mean_ret=0.0)
    verdict = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                            gate=GATE, port=PORT, headline_cost_bps=30,
                            horizons=[10], seed=42, spy_annual_return=0.0)
    assert verdict.decision == "KILL"


def test_too_few_events_kills():
    events = make_events("form4", 5, mean_ret=0.05)
    verdict = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                            gate=GATE, port=PORT, headline_cost_bps=30,
                            horizons=[10], seed=42, spy_annual_return=0.0)
    assert verdict.decision == "KILL"
    assert "events" in verdict.reasons[0]


def test_low_coverage_taints_verdict():
    events = make_events("form4", 200, mean_ret=0.03)
    verdict = evaluate_gate(events, coverage_rate=0.5, profile="form4",
                            gate=GATE, port=PORT, headline_cost_bps=30,
                            horizons=[10], seed=42, spy_annual_return=0.0)
    assert verdict.decision == "SUSPECT-GO"


def test_markdown_renders_both_profiles():
    events = make_events("form4", 200, mean_ret=0.03)
    v = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                      gate=GATE, port=PORT, headline_cost_bps=30,
                      horizons=[10], seed=42, spy_annual_return=0.0)
    md = render_markdown([v], events)
    assert "form4" in md and "GO" in md and "Coverage" in md
