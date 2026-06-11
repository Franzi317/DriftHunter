from datetime import date

from drifthunter.config import Sc13dConfig
from drifthunter.scorer.models import ThirteenDFiling
from drifthunter.scorer.sc13d import detect_signals


def filing(amend=False, ticker="EXM", filer="0009999999"):
    return ThirteenDFiling(
        accession="0002-24-000002",
        subject_cik="0000789019",
        subject_name="EXAMPLECO",
        ticker=ticker,
        filer_cik=filer,
        filer_name="SOME LP",
        filing_date=date(2024, 5, 6),
        is_amendment=amend,
    )


def test_new_13d_fires():
    signals = detect_signals([filing()], Sc13dConfig(activist_ciks=[]))
    assert len(signals) == 1
    assert signals[0].profile == "sc13d"
    assert signals[0].score == 2.0


def test_amendment_excluded():
    assert detect_signals([filing(amend=True)], Sc13dConfig(activist_ciks=[])) == []


def test_missing_ticker_excluded():
    assert detect_signals([filing(ticker=None)], Sc13dConfig(activist_ciks=[])) == []


def test_activist_filer_boost():
    signals = detect_signals([filing(filer="0001336528")],
                             Sc13dConfig(activist_ciks=["0001336528"]))
    assert signals[0].score == 3.0
