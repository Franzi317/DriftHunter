"""Per-event excess returns over a horizon x cost sweep (spec Sec3.4).

Entry: open of the first trading day strictly after trigger_date (trading
days = the ticker's own price index). Exit: open `horizon` rows later.
excess = (raw - cost) - SPY return over the identical dates.

ADV filter uses up to `adv_window` rows before entry; events with at least
one prior row are screened on the partial window (requiring the full window
would discard every signal early in the study period).

Prices are fetched once per ticker (and once for the benchmark) spanning all
of that ticker's events -- not once per event -- to keep provider/network
load linear in tickers, not signals.

Row order is the input signal order: callers that want a specific ordering
(e.g. by trigger_date) should sort `signals` before calling. Within a
signal, rows appear in (horizon, cost_bps) order matching the `horizons` and
`cost_bps_list` arguments. Filtered/uncovered events still emit one row per
(horizon, cost_bps) combination, with NaN returns and NaT dates.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from drifthunter.config import TradabilityConfig
from drifthunter.prices.provider import PriceProvider
from drifthunter.scorer.models import Signal

LOOKBACK_CAL_DAYS = 60
LOOKAHEAD_CAL_DAYS = 90

RESULT_COLUMNS = [
    "profile", "ticker", "trigger_date", "score", "horizon", "cost_bps",
    "entry_date", "exit_date", "raw_return", "excess_return", "filter_reason",
]


def _empty_row(sig: Signal, horizon: int, cost_bps: int, reason: str) -> dict:
    return {
        "profile": sig.profile,
        "ticker": sig.ticker,
        "trigger_date": sig.trigger_date,
        "score": sig.score,
        "horizon": horizon,
        "cost_bps": cost_bps,
        "entry_date": pd.NaT,
        "exit_date": pd.NaT,
        "raw_return": float("nan"),
        "excess_return": float("nan"),
        "filter_reason": reason,
    }


def _filtered_rows(sig: Signal, horizons: list[int], cost_bps_list: list[int],
                    reason: str) -> list[dict]:
    """All (horizon, cost_bps) combos for `sig`, each marked with `reason`."""
    return [
        _empty_row(sig, h, c, reason)
        for h in horizons
        for c in cost_bps_list
    ]


def _horizon_filtered_rows(sig: Signal, horizon: int, cost_bps_list: list[int],
                            reason: str) -> list[dict]:
    """All cost_bps combos for one `horizon` of `sig`, marked with `reason`."""
    return [_empty_row(sig, horizon, c, reason) for c in cost_bps_list]


def _event_rows(sig: Signal, px: pd.DataFrame, spy: pd.DataFrame,
                 trad: TradabilityConfig, horizons: list[int],
                 cost_bps_list: list[int]) -> list[dict]:
    if px.empty or spy.empty:
        return _filtered_rows(sig, horizons, cost_bps_list, "no_prices")

    trigger_ts = pd.Timestamp(sig.trigger_date)
    future = px[px.index > trigger_ts]
    if future.empty:
        return _filtered_rows(sig, horizons, cost_bps_list, "insufficient_history")

    entry_date = future.index[0]
    entry_pos = px.index.get_loc(entry_date)
    entry_open = float(px.iloc[entry_pos]["open"])

    if entry_open < trad.min_price:
        return _filtered_rows(sig, horizons, cost_bps_list, "min_price")

    pre = px.iloc[max(0, entry_pos - trad.adv_window):entry_pos]
    if len(pre) == 0:
        return _filtered_rows(sig, horizons, cost_bps_list, "insufficient_history")

    adv = float((pre["close"] * pre["volume"]).mean())
    if adv < trad.min_avg_dollar_volume:
        return _filtered_rows(sig, horizons, cost_bps_list, "adv")

    rows: list[dict] = []
    for horizon in horizons:
        exit_pos = entry_pos + horizon
        if exit_pos >= len(px):
            rows.extend(_horizon_filtered_rows(sig, horizon, cost_bps_list, "insufficient_history"))
            continue

        exit_date = px.index[exit_pos]
        exit_open = float(px.iloc[exit_pos]["open"])
        raw_return = exit_open / entry_open - 1.0

        if entry_date not in spy.index or exit_date not in spy.index:
            rows.extend(_horizon_filtered_rows(sig, horizon, cost_bps_list, "benchmark_gap"))
            continue

        spy_entry_open = float(spy.loc[entry_date, "open"])
        spy_exit_open = float(spy.loc[exit_date, "open"])
        spy_return = spy_exit_open / spy_entry_open - 1.0

        for cost_bps in cost_bps_list:
            net_return = raw_return - cost_bps / 10_000
            excess_return = net_return - spy_return
            rows.append({
                "profile": sig.profile,
                "ticker": sig.ticker,
                "trigger_date": sig.trigger_date,
                "score": sig.score,
                "horizon": horizon,
                "cost_bps": cost_bps,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "raw_return": raw_return,
                "excess_return": excess_return,
                "filter_reason": "",
            })

    return rows


def run_event_study(signals: list[Signal], provider: PriceProvider,
                     trad: TradabilityConfig, horizons: list[int],
                     cost_bps_list: list[int], benchmark: str) -> pd.DataFrame:
    """Run the event study over `signals`, returning one row per
    (signal, horizon, cost_bps) combination, in input signal order.

    Prices are fetched once per distinct ticker (spanning that ticker's
    events) and once for `benchmark` (spanning the global span), regardless
    of how many signals/events use them.
    """
    rows: list[dict] = []

    if not signals:
        return pd.DataFrame(rows, columns=RESULT_COLUMNS)

    max_horizon = max(horizons) if horizons else 0

    by_ticker: dict[str, list[Signal]] = {}
    for sig in signals:
        by_ticker.setdefault(sig.ticker, []).append(sig)

    ticker_frames: dict[str, pd.DataFrame] = {}
    ticker_ranges: dict[str, tuple[date, date]] = {}
    for ticker, sigs in by_ticker.items():
        triggers = [s.trigger_date for s in sigs]
        start = min(triggers) - timedelta(days=LOOKBACK_CAL_DAYS)
        end = max(triggers) + timedelta(days=LOOKAHEAD_CAL_DAYS + 2 * max_horizon)
        ticker_ranges[ticker] = (start, end)
        ticker_frames[ticker] = provider.daily(ticker, start, end)

    global_start = min(s for s, _ in ticker_ranges.values())
    global_end = max(e for _, e in ticker_ranges.values())
    spy = provider.daily(benchmark, global_start, global_end)

    for sig in signals:
        px = ticker_frames[sig.ticker]
        rows.extend(_event_rows(sig, px, spy, trad, horizons, cost_bps_list))

    return pd.DataFrame(rows, columns=RESULT_COLUMNS)
