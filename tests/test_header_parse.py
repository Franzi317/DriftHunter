from pathlib import Path

from drifthunter.ingest.header_parse import parse_header

FIXTURE = Path(__file__).parent / "fixtures" / "sc13d_header.txt"


def test_parses_subject_and_filer():
    hdr = parse_header(FIXTURE.read_text())
    assert hdr.subject_cik == "0000789019"
    assert hdr.subject_name == "EXAMPLECO"
    assert hdr.filer_cik == "0001336528"
    assert hdr.filer_name == "ACTIVIST CAPITAL LP"
    assert hdr.accession == "0001336528-24-000012"
