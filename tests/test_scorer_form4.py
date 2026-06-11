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


def test_score_increases_with_ceo_in_cluster_at_fire_time():
    small = detect_signals([buy("A", 1), buy("B", 5)], CFG)[0]
    big = detect_signals([buy("A", 1), buy("B", 5, ceo=True)], CFG)[0]
    assert big.score > small.score


def test_same_day_buys_batch_into_one_signal():
    # Three insiders, two filing on the same day: one signal, all three counted.
    signals = detect_signals(
        [buy("A", 1), buy("B", 5), buy("C", 5, ceo=True)], CFG
    )
    assert len(signals) == 1
    assert signals[0].trigger_date == date(2024, 3, 5)
    assert "insiders=3" in signals[0].detail
    assert signals[0].score > 4.0  # 3 insiders + log10(90k/25k) + 1.0 CEO bonus


def test_second_signal_after_suppression_lapses():
    # First cluster A(Mar1)+B(Mar4) fires Mar 4 (window {A,B}, both within
    # 10 bdays). Suppression then lasts until 10 bdays have elapsed since
    # Mar 4: busday_count(Mar4, Mar19) == 11 >= 10, so suppression has
    # lapsed by Mar 19.
    #
    # At Mar 19, A and B are both >10 bdays away (busday_count(Mar1,Mar19)=12,
    # busday_count(Mar4,Mar19)=11), so the window is just {D} -> 1 insider,
    # no fire. At Mar 21, the window is {D,E} (A and B still out of range)
    # -> 2 insiders, fires.
    signals = detect_signals(
        [buy("A", 1), buy("B", 4), buy("D", 19), buy("E", 21)], CFG
    )
    assert len(signals) == 2
    assert signals[0].trigger_date == date(2024, 3, 4)
    assert signals[1].trigger_date == date(2024, 3, 21)
    assert "insiders=2" in signals[1].detail


def test_issuers_scored_independently():
    # Two issuers, interleaved input order: each fires its own signal; output
    # sorted by (trigger_date, ticker).
    signals = detect_signals(
        [buy("A", 1, issuer="0000002", ticker="ZZZ"),
         buy("X", 4, issuer="0000001", ticker="AAA"),
         buy("B", 5, issuer="0000002", ticker="ZZZ"),
         buy("Y", 5, issuer="0000001", ticker="AAA")],
        CFG,
    )
    assert [(s.ticker, s.trigger_date) for s in signals] == [
        ("AAA", date(2024, 3, 5)), ("ZZZ", date(2024, 3, 5)),
    ]


def test_duplicate_insider_in_firing_window_not_double_counted():
    # A buys twice; C is a second distinct insider -> fires with insiders=2,
    # but total includes all three buys.
    signals = detect_signals([buy("A", 1), buy("A", 4), buy("C", 5)], CFG)
    assert len(signals) == 1
    assert "insiders=2" in signals[0].detail
    assert "total=$90,000" in signals[0].detail
