"""Form 4 cluster-buy detection. Deterministic; shared by backtest and live.

Business-day windows use numpy weekday counting (holidays deliberately
ignored: a holiday inside the window widens it by at most a day, which is
noise relative to the 10-day window, and keeps live/backtest behavior
identical with no calendar dependency).

Clustering algorithm
---------------------
Per issuer, eligible buys (value >= min_transaction_value, ticker present)
are sorted by filing date and grouped into *maximal chains*: consecutive
buys (by filing date) are joined into the same chain whenever they are no
more than ``cluster_window_bdays`` business days apart. A gap larger than
the window starts a new chain.

Each chain is evaluated as a whole against the fire conditions (>=2 distinct
insiders, a qualifying single CEO/CFO buy, or a qualifying single buy of any
size). If it fires, exactly one signal is emitted, dated on the *last*
buy's filing date in the chain.

Chains never overlap by construction (any two buys in different chains are
more than ``cluster_window_bdays`` apart), which already gives "one signal
per issuer per window" / anti-churn suppression for free -- a chain that
fires cannot be immediately followed by another chain that also covers some
of the same buys.

Note on causality: this means a signal is only emitted once the chain has
"settled" -- i.e. once a subsequent eligible buy (if any) falls outside the
window of the previous one, or there is no more data. A live system should
therefore evaluate an issuer's open chain once ``cluster_window_bdays`` have
elapsed since its most recent eligible buy with no new eligible buys having
arrived (or immediately if a new buy starts a fresh chain). This avoids
firing prematurely on a partial cluster and then suppressing a stronger
signal that arrives a day later.
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
        issuer_buys.sort(key=lambda b: (b.filing_date, b.accession))

        # Split into maximal chains: a new chain starts whenever the gap to
        # the previous buy exceeds cluster_window_bdays.
        chains: list[list[InsiderBuy]] = []
        for b in issuer_buys:
            if chains and _bdays_between(chains[-1][-1].filing_date, b.filing_date) <= cfg.cluster_window_bdays:
                chains[-1].append(b)
            else:
                chains.append([b])

        for chain in chains:
            insiders = {w.insider_cik for w in chain}
            total = sum(w.value for w in chain)
            any_ceo = any(w.is_ceo_cfo for w in chain)
            last = chain[-1]
            fires = (
                len(insiders) >= cfg.cluster_min_insiders
                or any(w.is_ceo_cfo and w.value >= cfg.ceo_cfo_single_min for w in chain)
                or any(w.value >= cfg.any_single_min for w in chain)
            )
            if not fires:
                continue
            score = (
                float(len(insiders))
                + math.log10(total / cfg.min_transaction_value)
                + (1.0 if any_ceo else 0.0)
            )
            signals.append(Signal(
                profile="form4",
                ticker=last.ticker,  # type: ignore[arg-type]  # filtered above
                trigger_date=last.filing_date,
                score=round(score, 4),
                detail=f"insiders={len(insiders)} total=${total:,.0f} ceo_cfo={any_ceo}",
            ))
    signals.sort(key=lambda s: (s.trigger_date, s.ticker))
    return signals
