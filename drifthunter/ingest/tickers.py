from __future__ import annotations

from dataclasses import dataclass
import json

from drifthunter.ingest.http import EdgarClient

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


@dataclass(frozen=True)
class TickerMap:
    _by_cik: dict[int, tuple[str, str]]  # cik -> (ticker, exchange)

    @classmethod
    def from_json(cls, raw: dict) -> TickerMap:
        """Build the cik -> (ticker, exchange) map.

        SEC's company_tickers_exchange.json lists MULTIPLE rows per company
        when it has more than one listed security (common stock, warrants
        like "AAPLW", units like "AAPLU", preferred/class shares, etc.). When
        a CIK has multiple rows, prefer the most-common-stock-looking ticker:
        the shortest ticker wins (common stock tickers are typically shorter
        than their warrant/unit counterparts), with ties broken
        alphabetically (e.g. "BRK.A" vs "BRK.B" -> "BRK.A").
        """
        idx = {f: i for i, f in enumerate(raw["fields"])}
        by_cik: dict[int, tuple[str, str]] = {}
        for row in raw["data"]:
            cik = int(row[idx["cik"]])
            ticker = str(row[idx["ticker"]])
            exchange = str(row[idx["exchange"]])
            existing = by_cik.get(cik)
            if existing is None or (len(ticker), ticker) < (len(existing[0]), existing[0]):
                by_cik[cik] = (ticker, exchange)
        return cls(_by_cik=by_cik)

    @classmethod
    def fetch(cls, client: EdgarClient) -> TickerMap:
        return cls.from_json(json.loads(client.get_bytes(TICKERS_URL, "company_tickers_exchange.json")))

    def ticker_for_cik(self, cik: str | int) -> str | None:
        entry = self._by_cik.get(int(cik))
        return entry[0] if entry else None

    def exchange_for_cik(self, cik: str | int) -> str | None:
        entry = self._by_cik.get(int(cik))
        return entry[1] if entry else None

    def is_listed(self, cik: str | int, allowed_exchanges: list[str]) -> bool:
        return self.exchange_for_cik(cik) in allowed_exchanges
