from datetime import date

from drifthunter.scorer.models import InsiderBuy, Signal, ThirteenDFiling


def test_insider_buy_value():
    buy = InsiderBuy(
        accession="0001-24-000001",
        issuer_cik="0000320193",
        ticker="AAPL",
        insider_cik="0001214156",
        insider_name="DOE JANE",
        is_ceo_cfo=True,
        trans_date=date(2024, 3, 1),
        filing_date=date(2024, 3, 4),
        shares=1000.0,
        price=30.0,
    )
    assert buy.value == 30000.0


def test_thirteen_d_amendment_flag():
    f = ThirteenDFiling(
        accession="0002-24-000002",
        subject_cik="0000789019",
        subject_name="EXAMPLECO",
        ticker="EXM",
        filer_cik="0001336528",
        filer_name="ACTIVIST LP",
        filing_date=date(2024, 5, 6),
        is_amendment=True,
    )
    assert f.is_amendment


def test_signal_fields():
    s = Signal(profile="form4", ticker="EXM", trigger_date=date(2024, 5, 6),
               score=3.2, detail="cluster=2 total=$120,000")
    assert s.profile == "form4"
    assert s.score > 0
