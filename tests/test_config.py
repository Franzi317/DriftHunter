from pathlib import Path

from drifthunter.config import load_config


def test_load_config_reads_repo_yaml():
    cfg = load_config(Path(__file__).parents[1] / "config.yaml")
    assert cfg.form4.min_transaction_value == 25000
    assert cfg.form4.cluster_window_bdays == 10
    assert cfg.study.horizons == [5, 10, 20, 40]
    assert cfg.study.headline_cost_bps == 30
    assert cfg.gate.form4_min_events == 300
    assert cfg.tradability.min_price == 2.0
    assert cfg.portfolio.max_positions == 5
    assert "drifthunter" in cfg.edgar.user_agent.lower()
