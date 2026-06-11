from datetime import date
from pathlib import Path

import pytest

from drifthunter.config import Form4Config
from drifthunter.ingest.insider_datasets import SkipCounts, _normalize_ticker, load_quarter_dir

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "form345"

CFG = Form4Config(
    min_transaction_value=25000, cluster_window_bdays=10, cluster_min_insiders=2,
    ceo_cfo_single_min=250000, any_single_min=1000000,
    ceo_cfo_title_keywords=["chief executive", "ceo", "chief financial", "cfo"],
)


def test_loads_only_code_p_acquisitions_on_form_4():
    buys = load_quarter_dir(FIXTURE_DIR, CFG)
    # P/A rows: accessions ...001 (3000sh), ...002 (5000sh), ...002 (100sh). The S/D AAPL row is excluded.
    assert len(buys) == 3
    assert all(b.issuer_cik == "789019" for b in buys)


def test_ceo_title_detection():
    buys = load_quarter_dir(FIXTURE_DIR, CFG)
    by_acc = {b.accession: b for b in buys if b.accession == "0001-24-000001"}
    assert by_acc["0001-24-000001"].is_ceo_cfo is True


def test_dates_and_values_parse():
    buys = load_quarter_dir(FIXTURE_DIR, CFG)
    first = next(b for b in buys if b.accession == "0001-24-000001")
    assert first.filing_date == date(2024, 3, 4)
    assert first.trans_date == date(2024, 3, 1)
    assert first.value == 30000.0
    assert first.ticker == "EXM"


def test_director_without_title_is_not_ceo_cfo():
    buys = load_quarter_dir(FIXTURE_DIR, CFG)
    director = next(b for b in buys if b.accession == "0001-24-000002")
    assert director.is_ceo_cfo is False


def test_joint_filing_collapses_to_one_buy(tmp_path):
    # One accession, one P/A transaction, THREE owner rows (joint filing):
    # must yield exactly ONE InsiderBuy carrying the transaction value once,
    # primary insider = smallest RPTOWNERCIK (by integer value), is_ceo_cfo
    # from ANY owner.
    import shutil
    fix = Path(__file__).parent / "fixtures" / "form345"
    work = tmp_path / "form345"
    work.mkdir()
    for name in ("SUBMISSION.tsv", "NONDERIV_TRANS.tsv", "REPORTINGOWNER.tsv"):
        shutil.copy(fix / name, work / name)
    with (work / "REPORTINGOWNER.tsv").open("a", encoding="utf-8") as f:
        f.write("0001-24-000001\t0999999\tFUND GP LLC\tOfficer\tChief Financial Officer\n")
        f.write("0001-24-000001\t5555555\tSIDE FUND LP\t10% Owner\t\n")
    buys = load_quarter_dir(work, CFG)
    ones = [b for b in buys if b.accession == "0001-24-000001"]
    assert len(ones) == 1
    # Owners on accession 0001-24-000001: 1111111 DOE JANE (existing fixture
    # row, CEO), 0999999 FUND GP LLC (CFO), 5555555 SIDE FUND LP (10% Owner).
    # Smallest by INTEGER cik value: 999999 (0999999) < 1111111 < 5555555.
    assert ones[0].insider_cik == "0999999"   # smallest int cik of {1111111, 0999999, 5555555}
    assert ones[0].insider_name == "FUND GP LLC"
    assert ones[0].is_ceo_cfo is True          # CEO title on 1111111, CFO on 0999999
    assert ones[0].value == 30000.0            # counted once


def test_unparseable_price_skipped_and_counted():
    skips: list[SkipCounts] = []
    buys = load_quarter_dir(FIXTURE_DIR, CFG, skip_counts=skips)
    assert len(buys) == 3                      # blank-price row not loaded
    assert skips[0].unparseable_value == 1


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("NYSE:NYCB", "NYCB"),
        ("NASDAQ:DHC", "DHC"),
        ("NYSE/TRN", "TRN"),
        ("(SIRI)", "SIRI"),
        ("[FOA]", "FOA"),
        ("AREN]", "AREN"),
        ("NCLH]", "NCLH"),
        ("N O G", "NOG"),
        ("UHAL UHALB", "UHAL"),
        ("UHAL,UHALB", "UHAL"),
        ("CRDA CRDB", "CRDA"),
        ("CCIX U", "CCIX"),
        ("AAPL", "AAPL"),
        ("BRK.B", "BRK.B"),
        ("SZL.AX", "SZL.AX"),
        ("TIPWX", "TIPWX"),
        ("", None),
        ("NONE", None),
        ("N/A", None),
    ],
)
def test_normalize_ticker(raw, expected):
    assert _normalize_ticker(raw) == expected
