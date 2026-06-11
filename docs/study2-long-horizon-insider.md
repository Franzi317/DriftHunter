# Study 2 — Long-Horizon Information Content of Insider Clusters & 13Ds

**Date committed:** 2026-06-11 (BEFORE any Study 2 return statistic was computed)
**Status:** criteria frozen; results pending
**Relationship to Phase 0:** a NEW hypothesis, not a re-tune. Phase 0 tested
5–40 trading-day announcement drift and returned KILL. The academic literature's
stronger claim for insider purchases is **6–12 month** information content
(insiders act on fundamentals that take quarters to surface). This study tests
that claim on the same signal set at 60/125/250 trading-day horizons.

## What this study is and is not

- It answers a **knowledge question**: do these filings carry long-horizon
  information? Verdict labels are **SIGNAL / NO-SIGNAL** — not GO/KILL.
- Nothing gets built or traded on a SIGNAL verdict without a separate
  deployment spec with its own gates (capital design at 250-day holds is a
  different problem entirely).
- Multiple-testing acknowledgment: this is test #2 on this dataset family.
  The bar below is stricter than Phase 0's for that reason.

## Method (engine unchanged)

- Signals: the existing 11,451 from `data/signals.parquet` (untouched).
- Event study: `run_event_study` as-is; horizons **[60, 125, 250]** trading
  days; cost 30bp; benchmark SPY (primary), IWM (labeled secondary diagnostic
  only — it cannot flip a verdict).
- Prices: Sharadar SEP (active through ~2026-07-11), SPY/IWM via free path.
- Evaluation: standalone script (`scripts/study2_long_horizon.py`);
  Phase 0 gate code is not modified.
- Note: 250-day horizons cannot complete for signals after ~April 2025;
  completed-event counts shrink with horizon. Minimums apply per horizon.

## Pre-committed SIGNAL criteria (per profile; ALL must hold at ≥1 horizon)

1. **n ≥ 300** (form4) / **n ≥ 150** (sc13d) completed events at that horizon.
2. **99% bootstrap CI lower bound > 0** on mean per-event excess return after
   30bp costs vs SPY (alpha=0.01; seed 42; 10,000 iterations).
   (Stricter than Phase 0's 95% because overlapping long windows make the IID
   bootstrap anti-conservative, and because this is test #2.)
3. **Economic significance floor** on the mean excess at that horizon:
   ≥ +1.0% (60d), ≥ +2.0% (125d), ≥ +3.0% (250d).
4. **Positive mean excess in ≥ 3 of 5 signal-vintage calendar years.**
5. **Price coverage ≥ 80%** for the profile (else verdict is SUSPECT-*).

Anything failing all horizons on any criterion → **NO-SIGNAL**. Descriptive
portfolio statistics may be reported but are explicitly NOT verdict inputs.

## Pre-registered interpretations

- NO-SIGNAL both profiles: the book closes on filing-following entirely,
  including as a manual idea source. Expected base case.
- SIGNAL on form4 only: consistent with the literature; would justify (at
  most) a new deployment-design phase for slow, low-turnover position
  following — with fresh gates.
- SIGNAL on sc13d only: activist campaigns pay out long; same caveat.
- Any SIGNAL accompanied by a NEGATIVE IWM-relative mean at the same horizon
  must be flagged prominently: it would suggest size-regime exposure, not
  stock selection.
