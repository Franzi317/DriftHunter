from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import pandas as pd

COLUMNS = ["open", "close", "volume"]


class PriceProvider(Protocol):
    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame: ...


def _safe_key(ticker: str) -> str:
    """Filesystem-safe cache-key fragment; raw ticker is still used for fetching.

    EDGAR-sourced tickers can contain '/' (e.g. "BF/B") and other characters
    that are invalid or meaningful (path separators) on common filesystems.
    Replace any character outside [A-Za-z0-9._-] with '_'. Theoretical
    collisions (e.g. "BF/B" and "BF_B" both mapping to "BF_B") are accepted:
    distinct real tickers won't differ only by a slash.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", ticker)


class CachingProvider:
    """Wraps any provider with a per-ticker parquet cache (empty results cached too,
    as zero-row frames, so dead tickers aren't re-fetched)."""

    def __init__(self, inner: PriceProvider, cache_dir: Path):
        self._inner = inner
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        key = self._dir / f"{_safe_key(ticker)}_{start.isoformat()}_{end.isoformat()}.parquet"
        if key.exists():
            return pd.read_parquet(key)
        df = self._inner.daily(ticker, start, end)
        if not df.empty:
            df = df[COLUMNS].astype(float).sort_index()
            df = df[~df.index.duplicated(keep="first")]
            df.index = pd.DatetimeIndex(df.index, freq=None).rename("date")
        else:
            df = pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="date"))
        df.to_parquet(key)
        return df


@dataclass(frozen=True)
class CoverageReport:
    total: int
    covered: int
    missing: list[str]

    @property
    def rate(self) -> float:
        return self.covered / self.total if self.total else 0.0


def coverage_report(tickers: list[str], provider: PriceProvider,
                     start: date, end: date) -> CoverageReport:
    unique = sorted(set(tickers))
    missing = [t for t in unique if provider.daily(t, start, end).empty]
    return CoverageReport(total=len(unique), covered=len(unique) - len(missing),
                           missing=missing)
