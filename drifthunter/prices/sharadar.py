"""Sharadar Equity Prices (SEP) via Nasdaq Data Link — survivorship-bias-free.

Spec §3.1: primary Phase 0 price source. Subscribe for one month, run the
study (results land in the parquet cache), cancel.
"""
from __future__ import annotations

from datetime import date

import pandas as pd


class SharadarProvider:
    def __init__(self, api_key: str):
        self._api_key = api_key

    def _get_table(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        import nasdaqdatalink
        nasdaqdatalink.ApiConfig.api_key = self._api_key
        return nasdaqdatalink.get_table(
            "SHARADAR/SEP",
            ticker=ticker,
            date={"gte": start.isoformat(), "lte": end.isoformat()},
            paginate=True,
        )

    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        raw = self._get_table(ticker, start, end)
        if raw is None or raw.empty:
            return pd.DataFrame()
        out = raw[["date", "open", "close", "volume"]].copy()
        out["date"] = pd.to_datetime(out["date"])
        out = out.set_index("date").sort_index()
        out = out[["open", "close", "volume"]].astype(float).rename_axis("date")
        out.columns.name = None
        return out
