"""Load SEC Insider Transactions Data Sets (quarterly TSV bundles) into InsiderBuy.

Bundles: https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets
Each quarter is a ZIP containing SUBMISSION.tsv, NONDERIV_TRANS.tsv,
REPORTINGOWNER.tsv (and others we ignore).

Joint filings and the per-accession owner aggregate: a single Form 4 accession
can list MULTIPLE REPORTINGOWNER rows when several reporting persons co-file
one report for the same transaction(s) -- e.g. a fund, its general partner,
and the managing member jointly reporting one purchase. Naively merging
NONDERIV_TRANS x SUBMISSION x REPORTINGOWNER on ACCESSION_NUMBER fans out
1 transaction row x N owner rows into N InsiderBuy records, each carrying the
full transaction value. That both multi-counts dollar value in cluster totals
and makes a single joint purchase decision look like an N-insider cluster,
producing false cluster signals. To avoid this, owners are first collapsed to
one aggregate row per accession: insider_cik/insider_name come from the owner
with the smallest integer RPTOWNERCIK (a deterministic choice that lets
affiliated co-filers collapse to a single identity for cluster-distinctness
purposes), and is_ceo_cfo is True if ANY owner row for the accession matches
the CEO/CFO title keywords. This owner aggregate is then merged 1:1 with
transactions x submissions, yielding exactly one InsiderBuy per transaction
row regardless of how many reporting owners co-filed it.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from drifthunter.config import Form4Config
from drifthunter.ingest.http import EdgarClient
from drifthunter.scorer.models import InsiderBuy

DATASET_URL = (
    "https://www.sec.gov/files/structureddata/data/"
    "insider-transactions-data-sets/{label}_form345.zip"
)

# Column names per the SEC dataset documentation. If a real download's columns
# differ, fix these constants (verified by verify_real_quarter / Step 6).
COL_ACCESSION = "ACCESSION_NUMBER"
COL_FILING_DATE = "FILING_DATE"
COL_DOC_TYPE = "DOCUMENT_TYPE"
COL_ISSUER_CIK = "ISSUERCIK"
COL_ISSUER_TICKER = "ISSUERTRADINGSYMBOL"
COL_TRANS_DATE = "TRANS_DATE"
COL_TRANS_CODE = "TRANS_CODE"
COL_ACQ_DISP = "TRANS_ACQUIRED_DISP_CD"
COL_SHARES = "TRANS_SHARES"
COL_PRICE = "TRANS_PRICEPERSHARE"
COL_OWNER_CIK = "RPTOWNERCIK"
COL_OWNER_NAME = "RPTOWNERNAME"
COL_OWNER_TITLE = "RPTOWNER_TITLE"


def _read_tsv(path_or_buf) -> pd.DataFrame:
    return pd.read_csv(path_or_buf, sep="\t", dtype=str, keep_default_na=False)


def _parse_sec_date(s: str) -> date | None:
    s = s.strip()
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_ceo_cfo(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(k in t for k in keywords)


def _cik_sort_key(cik: str) -> tuple[int, int | str]:
    """Sort key for RPTOWNERCIK: numeric ciks sort by integer value (so
    unequal-length numeric strings compare correctly, e.g. "999" < "10000001"
    numerically despite the reverse being true lexicographically); any
    non-numeric cik sorts after all numeric ones, by string value.
    """
    try:
        return (0, int(cik))
    except ValueError:
        return (1, cik)


def _aggregate_owners(owners: pd.DataFrame, cfg: Form4Config) -> pd.DataFrame:
    """Collapse REPORTINGOWNER rows to one aggregate row per accession.

    insider_cik/insider_name come from the owner with the smallest integer
    RPTOWNERCIK (whitespace-stripped before comparison); is_ceo_cfo is True
    if ANY owner row for the accession has a CEO/CFO title.
    """
    df = owners.copy()
    df[COL_OWNER_CIK] = df[COL_OWNER_CIK].str.strip()
    titles = df[COL_OWNER_TITLE] if COL_OWNER_TITLE in df.columns else pd.Series("", index=df.index)
    df["_is_ceo_cfo"] = titles.apply(
        lambda t: _is_ceo_cfo(t, cfg.ceo_cfo_title_keywords)
    )
    df["_cik_sort_key"] = df[COL_OWNER_CIK].apply(_cik_sort_key)
    df = df.sort_values("_cik_sort_key", kind="stable")

    primary = df.groupby(COL_ACCESSION, as_index=False).first()[
        [COL_ACCESSION, COL_OWNER_CIK, COL_OWNER_NAME]
    ]
    any_ceo_cfo = df.groupby(COL_ACCESSION, as_index=False)["_is_ceo_cfo"].any()
    return primary.merge(any_ceo_cfo, on=COL_ACCESSION)


def _build_buys(sub: pd.DataFrame, trans: pd.DataFrame, owners: pd.DataFrame,
                cfg: Form4Config) -> list[InsiderBuy]:
    sub = sub[sub[COL_DOC_TYPE].str.strip() == "4"]
    trans = trans[
        (trans[COL_TRANS_CODE].str.strip() == "P")
        & (trans[COL_ACQ_DISP].str.strip() == "A")
    ]
    owner_agg = _aggregate_owners(owners, cfg)
    merged = trans.merge(sub, on=COL_ACCESSION).merge(owner_agg, on=COL_ACCESSION)
    buys: list[InsiderBuy] = []
    for d in merged.to_dict("records"):
        filing_date = _parse_sec_date(d[COL_FILING_DATE])
        trans_date = _parse_sec_date(d[COL_TRANS_DATE])
        try:
            shares = float(d[COL_SHARES])
            price = float(d[COL_PRICE])
        except ValueError:
            continue  # missing/footnoted price or shares: unscoreable, skip
        if filing_date is None or trans_date is None or shares <= 0 or price <= 0:
            continue
        ticker = d[COL_ISSUER_TICKER].strip().upper() or None
        if ticker in {"NONE", "N/A"}:
            ticker = None
        buys.append(InsiderBuy(
            accession=d[COL_ACCESSION].strip(),
            issuer_cik=d[COL_ISSUER_CIK].strip(),
            ticker=ticker,
            insider_cik=d[COL_OWNER_CIK].strip(),
            insider_name=d[COL_OWNER_NAME].strip(),
            is_ceo_cfo=bool(d["_is_ceo_cfo"]),
            trans_date=trans_date,
            filing_date=filing_date,
            shares=shares,
            price=price,
        ))
    return buys


def load_quarter_dir(dir_path: Path, cfg: Form4Config) -> list[InsiderBuy]:
    """Load from an extracted directory (used by tests and inspection)."""
    return _build_buys(
        _read_tsv(dir_path / "SUBMISSION.tsv"),
        _read_tsv(dir_path / "NONDERIV_TRANS.tsv"),
        _read_tsv(dir_path / "REPORTINGOWNER.tsv"),
        cfg,
    )


def load_quarter_zip(zip_bytes: bytes, cfg: Form4Config) -> list[InsiderBuy]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        def read(name: str) -> pd.DataFrame:
            return _read_tsv(io.BytesIO(zf.read(name)))
        return _build_buys(
            read("SUBMISSION.tsv"), read("NONDERIV_TRANS.tsv"),
            read("REPORTINGOWNER.tsv"), cfg,
        )


def quarter_labels(start: date, end: date) -> list[str]:
    """['2021q2', '2021q3', ...] covering [start, end]."""
    labels = []
    y, q = start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        labels.append(f"{y}q{q}")
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return labels


def download_quarters(client: EdgarClient, start: date, end: date,
                      cfg: Form4Config) -> list[InsiderBuy]:
    buys: list[InsiderBuy] = []
    for label in quarter_labels(start, end):
        raw = client.get_bytes(DATASET_URL.format(label=label), f"form345/{label}.zip")
        buys.extend(load_quarter_zip(raw, cfg))
    return buys
