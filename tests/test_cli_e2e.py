from datetime import date
from pathlib import Path

import pandas as pd

from drifthunter.config import load_config
from drifthunter.backtest.event_study import run_event_study
from drifthunter.backtest.report import evaluate_gate, render_markdown
from drifthunter.ingest.insider_datasets import load_quarter_dir
from drifthunter.scorer.form4 import detect_signals


class DictProvider:
    def __init__(self, frames):
        self.frames = frames

    def daily(self, ticker, start, end):
        return self.frames.get(ticker, pd.DataFrame())


def synthetic_prices(n=80, start="2024-02-01", price=10.0, vol=500_000.0):
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"open": price, "close": price, "volume": vol},
                        index=idx).rename_axis("date")


def test_pipeline_from_fixture_tsv_to_report(tmp_path):
    cfg = load_config(Path(__file__).parents[1] / "config.yaml")
    buys = load_quarter_dir(Path(__file__).parent / "fixtures" / "form345", cfg.form4)
    signals = detect_signals(buys, cfg.form4)
    assert len(signals) == 1  # fixture has a 2-insider EXM cluster

    frames = {"EXM": synthetic_prices(), "SPY": synthetic_prices(price=400.0, vol=1e7)}
    events = run_event_study(signals, DictProvider(frames), cfg.tradability,
                             horizons=cfg.study.horizons,
                             cost_bps_list=cfg.study.cost_bps_sweep,
                             benchmark=cfg.study.benchmark)
    assert not events.empty

    verdict = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                            gate=cfg.gate, port=cfg.portfolio,
                            headline_cost_bps=cfg.study.headline_cost_bps,
                            horizons=cfg.study.horizons, seed=cfg.study.seed,
                            spy_annual_return=0.0,
                            bootstrap_iterations=cfg.study.bootstrap_iterations)
    md = render_markdown([verdict], events)
    assert "Phase 0 Report" in md
    # Flat prices + costs => no edge: a real gate must kill this.
    assert verdict.decision == "KILL"


def test_study_is_deterministic():
    cfg = load_config(Path(__file__).parents[1] / "config.yaml")
    buys = load_quarter_dir(Path(__file__).parent / "fixtures" / "form345", cfg.form4)
    signals = detect_signals(buys, cfg.form4)
    frames = {"EXM": synthetic_prices(), "SPY": synthetic_prices(price=400.0, vol=1e7)}

    def run():
        return run_event_study(signals, DictProvider(frames), cfg.tradability,
                               horizons=cfg.study.horizons,
                               cost_bps_list=cfg.study.cost_bps_sweep,
                               benchmark=cfg.study.benchmark)

    pd.testing.assert_frame_equal(run(), run())  # spec §5: byte-identical


def test_cli_help_smoke():
    from click.testing import CliRunner
    from drifthunter.cli import cli
    runner = CliRunner()
    assert runner.invoke(cli, ["--help"]).exit_code == 0
    for cmd in ("signals", "study", "report"):
        assert runner.invoke(cli, [cmd, "--help"]).exit_code == 0
