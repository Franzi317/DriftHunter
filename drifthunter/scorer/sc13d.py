"""SC 13D initiation detection. Deterministic; shared by backtest and live."""
from __future__ import annotations

from drifthunter.config import Sc13dConfig
from drifthunter.scorer.models import Signal, ThirteenDFiling

BASE_SCORE = 2.0
ACTIVIST_BOOST = 1.0


def detect_signals(filings: list[ThirteenDFiling], cfg: Sc13dConfig) -> list[Signal]:
    activists = set(cfg.activist_ciks)
    signals = [
        Signal(
            profile="sc13d",
            ticker=f.ticker,  # type: ignore[arg-type]  # filtered below
            trigger_date=f.filing_date,
            score=BASE_SCORE + (ACTIVIST_BOOST if f.filer_cik in activists else 0.0),
            detail=f"filer={f.filer_name} subject={f.subject_name}",
        )
        for f in filings
        if not f.is_amendment and f.ticker
    ]
    signals.sort(key=lambda s: (s.trigger_date, s.ticker))
    return signals
