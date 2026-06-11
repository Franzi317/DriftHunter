from datetime import date
from pathlib import Path

from drifthunter.config import Form4Config
from drifthunter.ingest.insider_datasets import load_quarter_dir

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
