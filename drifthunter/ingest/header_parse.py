"""Parse the SGML header at the top of an EDGAR .txt submission.

We only need SUBJECT COMPANY and FILED BY blocks; fetch is bounded by
reading until </SEC-HEADER> (headers are within the first few KB).
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
    m = re.search(rf"{field}:\s*(.+)", block)
    return m.group(1).strip() if m else ""


def parse_header(text: str) -> FilingHeader:
    header = text.split("</SEC-HEADER>")[0]
    accession = _block_field(header, "ACCESSION NUMBER")

    def section(name: str) -> str:
        m = re.search(rf"{name}:.*?(?=\n[A-Z][A-Z ]+:|\Z)", header, re.DOTALL)
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
