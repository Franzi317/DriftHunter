"""Capital-constrained chronological replay (spec Sec3.5).

Replays COMPLETED event rows (`filter_reason == ""`) for ONE (horizon,
cost_bps) pair in chronological order, simulating a single book with a fixed
bankroll, fixed per-position sizing, a cap on concurrent open positions, and
a cap on new entries per calendar day. There is no bumping: when the book is
full, the cap is hit, or cash is insufficient, the candidate is skipped (and
the corresponding skip counter incremented) -- it never displaces an existing
position or gets queued for later.

Equity is tracked on a REALIZED basis only: cash plus the at-cost value of
open positions (i.e. positions are carried at their entry size, not marked to
market). The equity curve is only updated when a position is closed (exit
settles, cash changes) -- entries move cash into an open position at cost, so
realized equity is unchanged at entry time. Max drawdown is computed on this
realized equity curve. Daily mark-to-market of open positions is deliberately
out of scope for this simulator.

Exit settlement happens before same-day entries are considered: an exit with
`exit_date <= entry_date` of the candidate frees up capital/book space in
time for that day's entry. This is consistent with the event study's
exit-at-open semantics (the position is liquidated at the open of exit_date,
so its proceeds are available for that day's trading).

Within a calendar day, candidates are ordered by score descending (highest
score gets first crack at remaining capacity/cash), with ticker ascending as
a final tie-break for full determinism.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from drifthunter.config import PortfolioConfig


@dataclass
class SimResult:
    final_equity: float
    max_drawdown: float
    trades_taken: int
    skipped_full_book: int
    skipped_entry_cap: int
    skipped_no_cash: int
    tickers_traded: list[str] = field(default_factory=list)
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)


def simulate(events: pd.DataFrame, cfg: PortfolioConfig) -> SimResult:
    """events: completed rows for ONE (horizon, cost_bps) pair.
    Columns required: ticker, score, entry_date, exit_date, raw_return."""
    df = events[events["filter_reason"] == ""].copy()
    # Contract: completed rows (filter_reason == "") must carry real returns.
    # Fail loudly here rather than silently propagating NaN equity downstream.
    assert df["raw_return"].notna().all(), "completed rows must have non-NaN raw_return"
    df = df.sort_values(["entry_date", "score", "ticker"], ascending=[True, False, True])

    cash = cfg.bankroll
    open_positions: list[dict] = []   # {ticker, exit_date, size, ret}
    equity = cfg.bankroll
    peak = equity
    max_dd = 0.0
    curve: list[tuple[pd.Timestamp, float]] = []
    taken: list[str] = []
    skipped_full = skipped_cap = skipped_cash = 0
    entries_today: tuple[pd.Timestamp | None, int] = (None, 0)

    def settle_exits(now: pd.Timestamp):
        nonlocal cash, equity, peak, max_dd
        due = [p for p in open_positions if p["exit_date"] <= now]
        for p in due:
            open_positions.remove(p)
            proceeds = p["size"] * (1.0 + p["ret"])
            cash += proceeds
            equity = cash + sum(q["size"] for q in open_positions)
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
            curve.append((p["exit_date"], equity))

    for row in df.itertuples(index=False):
        settle_exits(row.entry_date)
        day_count = entries_today[1] if entries_today[0] == row.entry_date else 0
        if day_count >= cfg.max_entries_per_day:
            skipped_cap += 1
            continue
        if len(open_positions) >= cfg.max_positions:
            skipped_full += 1
            continue
        size = min(cfg.position_size, cash)
        if size < cfg.position_size * 0.5:
            skipped_cash += 1
            continue
        cash -= size
        open_positions.append({
            "ticker": row.ticker, "exit_date": row.exit_date,
            "size": size, "ret": row.raw_return,
        })
        taken.append(row.ticker)
        entries_today = (row.entry_date, day_count + 1)

    settle_exits(pd.Timestamp.max)
    return SimResult(
        final_equity=round(cash, 6),
        max_drawdown=round(max_dd, 6),
        trades_taken=len(taken),
        skipped_full_book=skipped_full,
        skipped_entry_cap=skipped_cap,
        skipped_no_cash=skipped_cash,
        tickers_traded=taken,
        equity_curve=curve,
    )
