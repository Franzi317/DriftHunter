"""Parse EDGAR quarterly form.idx for new SC 13D filings.

Index URL pattern:
https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx

EDGAR label transition: around December 2024, EDGAR renamed the
Schedule 13D form-type label from "SC 13D" to "SCHEDULE 13D" (and
"SC 13D/A" to "SCHEDULE 13D/A") as part of the amended 13D/G
structured-data rules. The 2024q4 index contains both labels; 2025q1
onward uses only the new label. Both labels are matched here and the
original label is preserved in IndexRow.form_type.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from drifthunter.ingest.http import EdgarClient

INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"


@dataclass(frozen=True)
class IndexRow:
    form_type: str
    filer_name: str
    # Unpadded CIK as it appears in form.idx (e.g. "1336528"), unlike
    # FilingHeader.subject_cik/filer_cik which are zero-padded to 10
    # digits (e.g. "0001336528"); not directly comparable without
    # normalizing one side.
    filer_cik: str
    date_filed: date
    path: str


def parse_form_idx(text: str) -> list[IndexRow]:
    rows: list[IndexRow] = []
    for line in text.splitlines():
        # trailing space: excludes 'SC 13D/A' and 'SCHEDULE 13D/A'
        if not (line.startswith("SC 13D ") or line.startswith("SCHEDULE 13D ")):
            continue
        # columns separated by runs of 2+ spaces
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form_type, name, cik, filed, path = parts[0], parts[1], parts[2], parts[3], parts[4]
        if form_type not in ("SC 13D", "SCHEDULE 13D"):
            continue
        rows.append(IndexRow(
            form_type=form_type,
            filer_name=name,
            filer_cik=cik,
            date_filed=datetime.strptime(filed, "%Y-%m-%d").date(),
            path=path,
        ))
    return rows


def fetch_quarter(client: EdgarClient, year: int, q: int) -> list[IndexRow]:
    raw = client.get_bytes(INDEX_URL.format(year=year, q=q), f"form_idx/{year}q{q}.idx")
    return parse_form_idx(raw.decode("latin-1"))
