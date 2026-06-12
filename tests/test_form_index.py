from datetime import date
from pathlib import Path

from drifthunter.ingest.form_index import parse_form_idx

FIXTURE = Path(__file__).parent / "fixtures" / "form.idx"


def test_extracts_only_new_sc13d_rows():
    rows = parse_form_idx(FIXTURE.read_text())
    # 2 old-label (SC 13D) + 1 new-label (SCHEDULE 13D);
    # SC 13D/A, SC 13G, and SCHEDULE 13D/A excluded
    assert len(rows) == 3
    assert rows[0].filer_name == "ACTIVIST CAPITAL LP"
    assert rows[0].form_type == "SC 13D"
    assert rows[0].date_filed == date(2024, 3, 4)
    assert rows[0].path == "edgar/data/789019/0001336528-24-000012.txt"
    assert rows[1].filer_name == "SECOND ACTIVIST LP"
    assert rows[1].form_type == "SC 13D"
    assert rows[2].filer_name == "111 Equity Group"
    assert rows[2].form_type == "SCHEDULE 13D"
    assert rows[2].date_filed == date(2025, 1, 6)
    assert rows[2].path == "edgar/data/2024448/0001398344-25-000246.txt"
    # Amendment exclusion for both label styles
    assert all(r.form_type not in ("SC 13D/A", "SCHEDULE 13D/A") for r in rows)
