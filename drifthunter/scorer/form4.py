"""Form 4 cluster-buy detection. Deterministic; shared by backtest and live.

Business-day windows use numpy weekday counting (holidays deliberately
ignored: a holiday inside the window widens it by at most a day, which is
noise relative to the 10-day window, and keeps live/backtest behavior
identical with no calendar dependency).

Day-batched eager firing
-------------------------
Per issuer, eligible buys (value >= min_transaction_value, ticker present)
are grouped by ``filing_date`` -- all buys filed on the same day are known
together (this matches live behavior, where entry queues to the next market
open after the filing day anyway, so same-day filings are all known before
entry).

Filing dates are walked in ascending order. For a given filing date D, the
*window* is every eligible buy for the issuer with a filing date within
``cluster_window_bdays`` business days before-or-equal D (inclusive of D
itself). The fire conditions are evaluated over this window:

- distinct insiders in the window >= cluster_min_insiders, or
- a qualifying single CEO/CFO buy in the window, or
- a qualifying single buy of any size in the window.

If the window fires and the issuer is not currently suppressed, exactly one
signal is emitted, dated D, scored over the window, and the issuer is then
suppressed until ``cluster_window_bdays`` business days have elapsed since D
-- giving "one signal per issuer per window" / anti-churn suppression.

Note on causality: the live system evaluates this same day-batched window as
of each filing day and enters at the next market open -- there is no
settling wait. A signal fires the moment its window's conditions are met,
using only information available as of that filing day.

Complexity note: the window is rebuilt from scratch for each filing date
(O(k) per date, O(k*m) per issuer for k buys and m distinct filing dates),
which is acceptable at backtest scale. A sliding-window optimization is
possible without changing semantics if heavy-tail issuers (very large k)
make this too slow.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np

from drifthunter.config import Form4Config
from drifthunter.scorer.models import InsiderBuy, Signal


def _bdays_between(start: date, end: date) -> int:
    return int(np.busday_count(start.isoformat(), end.isoformat()))


def detect_signals(buys: list[InsiderBuy], cfg: Form4Config) -> list[Signal]:
    eligible = [
        b for b in buys
        if b.value >= cfg.min_transaction_value and b.ticker
    ]
    by_issuer: dict[str, list[InsiderBuy]] = {}
    for b in eligible:
        by_issuer.setdefault(b.issuer_cik, []).append(b)

    signals: list[Signal] = []
    for issuer_buys in by_issuer.values():
        # Group eligible buys by filing date (day-batching).
        by_date: dict[date, list[InsiderBuy]] = {}
        for b in issuer_buys:
            by_date.setdefault(b.filing_date, []).append(b)

        filing_dates = sorted(by_date.keys())

        suppressed_until: date | None = None
        for d in filing_dates:
            if suppressed_until is not None and _bdays_between(suppressed_until, d) < cfg.cluster_window_bdays:
                continue

            window = [
                b for b in issuer_buys
                # 0 <= ... excludes future filings (causality); upper bound is the
                # trailing window
                if 0 <= _bdays_between(b.filing_date, d) <= cfg.cluster_window_bdays
            ]

            insiders = {w.insider_cik for w in window}
            total = sum(w.value for w in window)
            # any_ceo (any-size CEO/CFO buy in the window) grants the score bonus
            # below; the FIRE condition below requires a CEO/CFO buy whose value
            # is >= ceo_cfo_single_min. Two different predicates by design.
            any_ceo = any(w.is_ceo_cfo for w in window)
            fires = (
                len(insiders) >= cfg.cluster_min_insiders
                or any(w.is_ceo_cfo and w.value >= cfg.ceo_cfo_single_min for w in window)
                or any(w.value >= cfg.any_single_min for w in window)
            )
            if not fires:
                continue

            latest = max(by_date[d], key=lambda b: (b.filing_date, b.accession))
            score = (
                float(len(insiders))
                + math.log10(total / cfg.min_transaction_value)
                + (1.0 if any_ceo else 0.0)
            )
            signals.append(Signal(
                profile="form4",
                ticker=latest.ticker,  # type: ignore[arg-type]  # filtered above
                trigger_date=d,
                score=round(score, 4),
                detail=f"insiders={len(insiders)} total=${total:,.0f} ceo_cfo={any_ceo}",
            ))
            suppressed_until = d

    signals.sort(key=lambda s: (s.trigger_date, s.ticker))
    return signals
