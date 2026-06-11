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
        idx = {f: i for i, f in enumerate(raw["fields"])}
        by_cik = {
            int(row[idx["cik"]]): (str(row[idx["ticker"]]), str(row[idx["exchange"]]))
            for row in raw["data"]
        }
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
