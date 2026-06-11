from datetime import date
from pathlib import Path

from drifthunter.ingest.form_index import parse_form_idx

FIXTURE = Path(__file__).parent / "fixtures" / "form.idx"


def test_extracts_only_new_sc13d_rows():
    rows = parse_form_idx(FIXTURE.read_text())
    assert len(rows) == 2  # SC 13D/A and SC 13G excluded
    assert rows[0].filer_name == "ACTIVIST CAPITAL LP"
    assert rows[0].date_filed == date(2024, 3, 4)
    assert rows[0].path == "edgar/data/789019/0001336528-24-000012.txt"
