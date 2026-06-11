import json
from pathlib import Path

from drifthunter.ingest.tickers import TickerMap

FIXTURE = Path(__file__).parent / "fixtures" / "company_tickers_exchange.json"


def test_lookup_by_cik_zero_padded_or_int():
    tm = TickerMap.from_json(json.loads(FIXTURE.read_text()))
    assert tm.ticker_for_cik("0000320193") == "AAPL"
    assert tm.ticker_for_cik("320193") == "AAPL"
    assert tm.exchange_for_cik("0000789019") == "NYSE"


def test_unknown_cik_returns_none():
    tm = TickerMap.from_json(json.loads(FIXTURE.read_text()))
    assert tm.ticker_for_cik("0000000001") is None


def test_listed_filter_excludes_otc():
    tm = TickerMap.from_json(json.loads(FIXTURE.read_text()))
    assert tm.is_listed("0000320193", ["NYSE", "Nasdaq"])
    assert not tm.is_listed("0001234567", ["NYSE", "Nasdaq"])
