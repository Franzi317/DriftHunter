from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class InsiderBuy:
    """One non-derivative open-market purchase (Form 4, code P)."""
    accession: str
    issuer_cik: str
    ticker: str | None
    insider_cik: str
    insider_name: str
    is_ceo_cfo: bool
    trans_date: date
    filing_date: date
    shares: float
    price: float

    @property
    def value(self) -> float:
        return self.shares * self.price


@dataclass(frozen=True)
class ThirteenDFiling:
    """One SC 13D (or 13D/A) filing, header-level data only."""
    accession: str
    subject_cik: str
    subject_name: str
    ticker: str | None
    filer_cik: str
    filer_name: str
    filing_date: date
    is_amendment: bool


@dataclass(frozen=True)
class Signal:
    """A scored trade candidate. profile is 'form4' or 'sc13d'."""
    profile: str
    ticker: str
    trigger_date: date
    score: float
    detail: str
