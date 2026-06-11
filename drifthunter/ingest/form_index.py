"""Parse EDGAR quarterly form.idx for new SC 13D filings.

Index URL pattern:
https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx
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
    filer_cik: str
    date_filed: date
    path: str


def parse_form_idx(text: str) -> list[IndexRow]:
    rows: list[IndexRow] = []
    for line in text.splitlines():
        if not line.startswith("SC 13D "):  # trailing space: excludes 'SC 13D/A'
            continue
        # columns separated by runs of 2+ spaces
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form_type, name, cik, filed, path = parts[0], parts[1], parts[2], parts[3], parts[4]
        if form_type != "SC 13D":
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
