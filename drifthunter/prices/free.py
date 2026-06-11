"""Free fallback chain: yfinance first, Stooq CSV second.

Spec §3.2: this path is the fallback; expect imperfect delisted-ticker
coverage and rely on the coverage report to quantify it.
"""
from __future__ import annotations

import io
from datetime import date

import httpx
import pandas as pd

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}.us&d1={d1}&d2={d2}&i=d"


class FreeProvider:
    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        df = self._yfinance(ticker, start, end)
        if df.empty:
            df = self._stooq(ticker, start, end)
        return df

    @staticmethod
    def _yfinance(ticker: str, start: date, end: date) -> pd.DataFrame:
        import yfinance as yf
        raw = yf.download(ticker, start=start.isoformat(), end=end.isoformat(),
                           progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            return pd.DataFrame()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        out = raw.rename(columns={"Open": "open", "Close": "close", "Volume": "volume"})
        out = out[["open", "close", "volume"]].astype(float).rename_axis("date")
        out.columns.name = None
        return out

    @staticmethod
    def _stooq(ticker: str, start: date, end: date) -> pd.DataFrame:
        url = STOOQ_URL.format(symbol=ticker.lower(),
                                d1=start.strftime("%Y%m%d"), d2=end.strftime("%Y%m%d"))
        try:
            resp = httpx.get(url, timeout=30.0)
            resp.raise_for_status()
        except httpx.HTTPError:
            return pd.DataFrame()
        if not resp.text.startswith("Date,"):
            return pd.DataFrame()
        raw = pd.read_csv(io.StringIO(resp.text), parse_dates=["Date"], index_col="Date")
        out = raw.rename(columns={"Open": "open", "Close": "close", "Volume": "volume"})
        out = out[["open", "close", "volume"]].astype(float).rename_axis("date")
        out.columns.name = None
        return out
