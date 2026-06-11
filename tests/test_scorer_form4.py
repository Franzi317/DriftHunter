from datetime import date

from drifthunter.config import Form4Config
from drifthunter.scorer.form4 import detect_signals
from drifthunter.scorer.models import InsiderBuy

CFG = Form4Config(
    min_transaction_value=25000,
    cluster_window_bdays=10,
    cluster_min_insiders=2,
    ceo_cfo_single_min=250000,
    any_single_min=1000000,
    ceo_cfo_title_keywords=["ceo", "cfo"],
)


def buy(insider="A", day=1, value=30000.0, ceo=False, issuer="0000001", ticker="TST"):
    return InsiderBuy(
        accession=f"acc-{insider}-{day}",
        issuer_cik=issuer,
        ticker=ticker,
        insider_cik=insider,
        insider_name=insider,
        is_ceo_cfo=ceo,
        trans_date=date(2024, 3, day),
        filing_date=date(2024, 3, day),
        shares=value / 10.0,
        price=10.0,
    )


def test_two_insiders_within_window_fire_cluster():
    signals = detect_signals([buy("A", 1), buy("B", 5)], CFG)
    assert len(signals) == 1
    assert signals[0].profile == "form4"
    assert signals[0].trigger_date == date(2024, 3, 5)  # fires on the later filing


def test_same_insider_twice_is_not_a_cluster():
    signals = detect_signals([buy("A", 1), buy("A", 5)], CFG)
    assert signals == []


def test_window_boundary_excludes_stale_buy():
    # Mar 1 2024 (Fri) -> Mar 15 (Fri) is exactly 10 business days; Mar 18 is 11.
    signals = detect_signals([buy("A", 1), buy("B", 18)], CFG)
    assert signals == []


def test_below_value_threshold_ineligible():
    signals = detect_signals([buy("A", 1, value=24999.0), buy("B", 5)], CFG)
    assert signals == []


def test_single_ceo_buy_over_250k_fires():
    signals = detect_signals([buy("A", 4, value=250000.0, ceo=True)], CFG)
    assert len(signals) == 1


def test_single_non_officer_needs_1m():
    assert detect_signals([buy("A", 4, value=999999.0)], CFG) == []
    assert len(detect_signals([buy("A", 4, value=1000000.0)], CFG)) == 1


def test_one_signal_per_issuer_per_window():
    # Three insiders buying across 4 days: cluster fires once, not on every new buy.
    signals = detect_signals([buy("A", 1), buy("B", 4), buy("C", 5)], CFG)
    assert len(signals) == 1


def test_missing_ticker_is_skipped():
    signals = detect_signals(
        [buy("A", 1, ticker=None), buy("B", 5, ticker=None)], CFG
    )
    assert signals == []


def test_score_increases_with_cluster_size_and_ceo():
    small = detect_signals([buy("A", 1), buy("B", 5)], CFG)[0]
    big = detect_signals(
        [buy("A", 1), buy("B", 4), buy("C", 5, ceo=True)], CFG
    )[0]
    assert big.score > small.score
