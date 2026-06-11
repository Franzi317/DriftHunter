"""GO/KILL gate evaluation (spec §3.6) and markdown report."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from drifthunter.backtest.portfolio import simulate
from drifthunter.backtest.stats import bootstrap_ci, yearly_means
from drifthunter.config import GateConfig, PortfolioConfig


@dataclass
class GateVerdict:
    profile: str
    decision: str                 # GO | KILL | SUSPECT-GO | SUSPECT-KILL
    best_horizon: int | None
    coverage_rate: float
    reasons: list[str] = field(default_factory=list)
    horizon_stats: list[dict] = field(default_factory=list)


def _annualized(total_return: float, years: float) -> float:
    if years <= 0:
        return 0.0
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def evaluate_gate(events: pd.DataFrame, coverage_rate: float, profile: str,
                  gate: GateConfig, port: PortfolioConfig, headline_cost_bps: int,
                  horizons: list[int], seed: int,
                  spy_annual_return: float,
                  bootstrap_iterations: int = 10_000) -> GateVerdict:
    """Evaluate the GO/KILL gate (spec §3.6) for a single profile.

    Benchmark contract: `spy_annual_return` MUST be the SPY CAGR computed
    from real prices over the same span as this profile's portfolio
    (entry_date.min -> exit_date.max). Passing 0.0 degrades the "portfolio
    excess > 0" check to "portfolio made money", which can soften a KILL
    into a GO in bull markets.
    """
    min_events = gate.form4_min_events if profile == "form4" else gate.sc13d_min_events
    df = events[(events["profile"] == profile)
                & (events["cost_bps"] == headline_cost_bps)
                & (events["filter_reason"] == "")]
    reasons: list[str] = []
    horizon_stats: list[dict] = []
    best: int | None = None

    for h in horizons:
        hdf = df[df["horizon"] == h]
        n = len(hdf)
        if n == 0:
            continue
        ci = bootstrap_ci(hdf["excess_return"].to_numpy(), n_iter=bootstrap_iterations, seed=seed)
        # yearly_means groups by trigger_date year (signal vintage), while the
        # annualization span below uses entry/exit dates (capital-deployed
        # window) -- this divergence is deliberate.
        years = yearly_means(hdf)
        positive_years = sum(1 for v in years.values() if v > 0)
        sim = simulate(hdf, port)
        span_years = max(
            (hdf["exit_date"].max() - hdf["entry_date"].min()).days / 365.25, 0.25
        )
        port_annual = _annualized(sim.final_equity / port.bankroll - 1.0, span_years)
        port_excess = port_annual - spy_annual_return
        checks = {
            f"n>={min_events} events": n >= min_events,
            "CI lo > 0": ci.lo > 0,
            f">={gate.min_years_positive} positive years": positive_years >= gate.min_years_positive,
            "portfolio excess > 0": port_excess > 0,
            f"max DD < {gate.max_drawdown:.0%}": sim.max_drawdown < gate.max_drawdown,
        }
        horizon_stats.append({
            "horizon": h, "n": n, "mean": ci.mean, "ci_lo": ci.lo, "ci_hi": ci.hi,
            "positive_years": positive_years, "portfolio_annual_excess": port_excess,
            "max_drawdown": sim.max_drawdown,
            "skipped_full_book": sim.skipped_full_book,
            "passed": all(checks.values()),
            "failed_checks": [k for k, ok in checks.items() if not ok],
        })
        if all(checks.values()) and best is None:
            best = h

    if not horizon_stats:
        reasons.append(f"0 completed events at {headline_cost_bps}bp (need {min_events} events)")
        decision = "KILL"
    elif best is not None:
        decision = "GO"
        reasons.append(f"horizon {best} passed all checks")
    else:
        decision = "KILL"
        worst = horizon_stats[0]
        reasons.extend(worst["failed_checks"] or ["no horizon passed"])

    if coverage_rate < gate.min_coverage:
        decision = f"SUSPECT-{decision}"
        reasons.append(f"coverage {coverage_rate:.0%} < {gate.min_coverage:.0%}")

    return GateVerdict(profile=profile, decision=decision, best_horizon=best,
                       coverage_rate=coverage_rate, reasons=reasons,
                       horizon_stats=horizon_stats)


def render_markdown(verdicts: list[GateVerdict], events: pd.DataFrame) -> str:
    lines = ["# DriftHunter Phase 0 Report", ""]
    for v in verdicts:
        lines += [f"## Profile: {v.profile} — **{v.decision}**", "",
                  f"Coverage: {v.coverage_rate:.1%}",
                  f"Reasons: {'; '.join(v.reasons)}"]
        if v.best_horizon is not None:
            lines.append(f"Best horizon: {v.best_horizon}")
        lines += ["",
                  "| Horizon | N | Mean excess | 95% CI | Pos. years | Port. excess (ann.) | Max DD | Skipped (full book) | Pass |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for s in v.horizon_stats:
            lines.append(
                f"| {s['horizon']} | {s['n']} | {s['mean']:+.4f} "
                f"| [{s['ci_lo']:+.4f}, {s['ci_hi']:+.4f}] | {s['positive_years']} "
                f"| {s['portfolio_annual_excess']:+.2%} | {s['max_drawdown']:.2%} "
                f"| {s['skipped_full_book']} | {'✅' if s['passed'] else '❌'} |"
            )
        lines.append("")
    lines += ["> Max DD is computed on the REALIZED equity curve (positions held at cost",
              "> until exit); it understates true intra-position peak-to-trough drawdown.",
              "> Treat the gate's drawdown check as a lower bound, not an estimate.", "",
              "> The bootstrap CI resamples per-event excess returns independently. Events with",
              "> overlapping holding windows share market-regime exposure, so the true CI is wider",
              "> than reported; treat \"CI lo > 0\" as necessary but not sufficient, and prefer a",
              "> comfortable margin over a marginal pass.", ""]
    filtered = events[events["filter_reason"] != ""]
    if not filtered.empty:
        lines += ["## Excluded events by reason", ""]
        for reason, group in filtered.groupby("filter_reason"):
            dedup = group.drop_duplicates(subset=["ticker", "trigger_date"])
            n_events = len(dedup)
            n_tickers = dedup["ticker"].nunique()
            lines.append(f"- {reason}: {n_events} events ({n_tickers} tickers)")
    return "\n".join(lines)
