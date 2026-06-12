# Phase 0 Runbook — Real Run & Verdict

**Run date:** 2026-06-11
**Operator:** Claude (subagent-driven), commissioned by Ryan Franzman
**Verdict: KILL — both profiles. Per spec §3.6, no live system will be built.**

## Data

| Item | Detail |
|---|---|
| Study window | 2021-04-01 → 2026-03-31 (20 quarters) |
| Form 4 source | SEC Insider Transactions Data Sets, 2021q2–2026q1 (~125k eligible buys) |
| SC 13D source | EDGAR full-index + filing headers (~3.4k new 13Ds; 0 blank-CIK headers) |
| Prices | Sharadar SEP (paid, survivorship-bias-free), one-month subscription; SPY benchmark via yfinance (SEP lacks ETFs) |
| Signals | form4: 6,448 → 6,431 after instrument filter; sc13d: 5,020 → joint total 11,451 |
| Event rows | 137,412 (signals × 4 horizons × 3 costs) |
| Coverage | form4 88.7%, sc13d 92.3% — both above the 80% bar; **no SUSPECT taint** |

## Process integrity notes

- A ticker-resolution data-quality fix (multi-listing CIKs resolving to warrant
  tickers; dirty Form 4 symbols; derivative-listing screen) was applied
  **before any return statistic was computed or viewed** (commit `222d51b`).
  It lifted sc13d coverage 78.4% → 92.3%. This was universe hygiene, not tuning.
- `activist_ciks` was empty for this run. The boost affects only same-day entry
  priority in the portfolio sim, not any gate check; immaterial to the verdict,
  and moot given the margin of failure.
- Gate thresholds were frozen in `config.yaml` before the run (spec §3.6).

## Results (headline cost 30bp, vs SPY)

**Form 4 cluster buys (n=4,651 completed events):** mean per-event excess is
indistinguishable from zero at 5–10 days (+0.04% / −0.03%, CIs straddle zero)
and significantly NEGATIVE at 20–40 days (−0.50% / −1.27%, CIs entirely below
zero). All four horizons fail ≥3 of 5 checks. Portfolio sims: −2.6% to −27.5%
annualized excess, realized drawdowns ~50%.

**13D initiations (n=2,976 completed events):** significantly negative at every
horizon: −1.6% (5d) to −7.3% (40d) mean excess, all CIs entirely below zero.
Activist targets underperformed SPY badly in this window.

Full tables: `data/report.md` (committed alongside this runbook).

## Interpretation

This is a decisive KILL, not a near-miss. The post-filing drift documented in
the academic literature did not survive 2021–2026 against a SPY benchmark at
30bp costs in this implementation. Plausible contributors: (a) the entry is the
next open after the filing day, i.e., after the announcement pop that contains
most of the historically documented reaction; (b) 2021–2026 was historically
hostile to small-caps vs a mega-cap-concentrated SPY, so any small-cap-tilted
strategy faced a structural excess-return headwind; (c) the anomaly may simply
be arbitraged away at retail-accessible latency. The gate does not distinguish
these — by design. A strategy that needs the benchmark changed to look good
fails the opportunity-cost test the spec encodes.

## Decision

Per the pre-committed criteria (spec §3.6): **stop here permanently.** The
backtest engine (100 passing tests) remains as a reusable research tool; any
future strategy idea gets a fresh spec with fresh pre-committed gates before
this codebase is pointed at it.

## Post-run actions

- [ ] **Cancel the Sharadar SEP subscription** before the monthly renewal
      (price data is parquet-cached locally; derived results are permanent).
- [x] Commit `data/report.md` + this runbook.
- [x] Merge `feature/phase0-backtest` to master.

## 2026-06-11 addendum — corrected 13D ingestion

During pre-publication review the sc13d sample looked wrong: its long-horizon
completed-event count was constant across horizons that should have shrunk.
Investigation traced this to ingestion, not analysis. When the amended 13D/G
rules took effect in December 2024, EDGAR renamed the Schedule 13D form-type
label in the full-index from `SC 13D` to `SCHEDULE 13D`, and the index parser
silently skipped the new label — roughly 1,900 initiations from December 2024
through March 2026 never entered the sample.

**Fix:** parser corrected to match both labels (commit `0390d2b`). Every
analysis (Phase 0 gate, post-mortem diagnostics, Study 2) was re-run on the
corrected sample under the **same pre-committed criteria — no gate threshold,
spec, or criterion was modified.**

**Before/after verdict (unchanged):** Phase 0 KILL/KILL (form4/sc13d); Study 2
NO-SIGNAL/NO-SIGNAL. All four verdicts identical to the original run.

**New headline sc13d figures (corrected run):**

- Sample: 5,020 → 7,244 signals; completed Phase-0 events 2,976 → 4,256;
  coverage 92.3% → 93.4%.
- Phase 0 (30bp vs SPY): mean excess −1.87% (5d), −2.60% (10d), −3.07% (20d),
  −7.44% (40d); all CIs entirely below zero.
- Study 2 (vs SPY): −9.60% (60d), −20.32% (125d), −29.32% (250d).
- form4 changed by +8 signals (a richer 13D subject-CIK map let a few more
  form4 tickers pass the shared exchange filter); immaterial to the verdict.

The committed reports (`data/report.md`, `data/diagnostics.md`,
`data/study2-report.md`) and the article reflect this corrected run. The gate
criteria were not modified — the correction could not become a re-tune.
