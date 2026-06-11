"""Parse the SGML header at the top of an EDGAR .txt submission.

We only need SUBJECT COMPANY and FILED BY blocks; fetch is bounded by
reading until </SEC-HEADER> (headers are within the first few KB).

Known limitations (scope decisions, not bugs):

- Multi-FILED-BY joint filings: only the FIRST "FILED BY:" block is
  captured, since FilingHeader has singular filer_cik/filer_name fields
  by design. For joint filings with multiple co-filers, activist-list
  scoring based on filer_cik/filer_name can miss a co-filer listed in a
  second (or later) "FILED BY:" block.
- CIK formats: FilingHeader.subject_cik / filer_cik are zero-padded
  10-digit strings (e.g. "0001336528") as they appear in the SGML
  header, whereas IndexRow.filer_cik (form_index.py) is unpadded (e.g.
  "1336528") as it appears in form.idx. These are not directly
  comparable without normalizing one to match the other.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from drifthunter.ingest.http import EdgarClient

ARCHIVES_BASE = "https://www.sec.gov/Archives/"


@dataclass(frozen=True)
class FilingHeader:
    accession: str
    subject_cik: str
    subject_name: str
    filer_cik: str
    filer_name: str


def _block_field(block: str, field: str) -> str:
    # Anchor the value to the SAME line as "FIELD:" so that a blank
    # value (header line ends right after the colon, possibly followed
    # by trailing whitespace) does not let `(.+)` cross the newline and
    # capture the next line's content.
    m = re.search(rf"^[ \t]*{re.escape(field)}:[ \t]*(\S.*)?$", block, re.MULTILINE)
    return (m.group(1) or "").strip() if m else ""


def parse_header(text: str) -> FilingHeader:
    header = text.split("</SEC-HEADER>")[0]
    accession = _block_field(header, "ACCESSION NUMBER")

    def section(name: str) -> str:
        m = re.search(
            rf"^{re.escape(name)}:.*?(?=\n[A-Z][A-Z ]+:|\Z)",
            header,
            re.DOTALL | re.MULTILINE,
        )
        return m.group(0) if m else ""

    subject = section("SUBJECT COMPANY")
    filed_by = section("FILED BY")
    return FilingHeader(
        accession=accession,
        subject_cik=_block_field(subject, "CENTRAL INDEX KEY"),
        subject_name=_block_field(subject, "COMPANY CONFORMED NAME"),
        filer_cik=_block_field(filed_by, "CENTRAL INDEX KEY"),
        filer_name=_block_field(filed_by, "COMPANY CONFORMED NAME"),
    )


def fetch_header(client: EdgarClient, path: str) -> FilingHeader:
    accession_key = path.replace("/", "_")
    raw = client.get_bytes(ARCHIVES_BASE + path, f"headers/{accession_key}")
    return parse_header(raw.decode("latin-1", errors="replace"))
