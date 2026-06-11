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


def test_blank_cik_yields_empty_not_garbage():
    hdr = parse_header((Path(__file__).parent / "fixtures" / "sc13d_header_blank_cik.txt").read_text())
    assert hdr.subject_cik == ""              # NOT next-line garbage
    assert hdr.subject_name == "EXAMPLECO"    # other fields unaffected
    assert hdr.filer_cik == "0001336528"


def test_reordered_sections_parse_independently():
    original = (Path(__file__).parent / "fixtures" / "sc13d_header.txt").read_text()
    # crude reorder: parse from a header where FILED BY appears before SUBJECT COMPANY
    head, rest = original.split("SUBJECT COMPANY:", 1)
    subject_block, tail = rest.split("FILED BY:", 1)
    reordered = head + "FILED BY:" + tail.split("</SEC-HEADER>")[0] + "SUBJECT COMPANY:" + subject_block + "</SEC-HEADER>\n<DOCUMENT>"
    hdr = parse_header(reordered)
    assert hdr.subject_cik == "0000789019"
    assert hdr.filer_cik == "0001336528"
