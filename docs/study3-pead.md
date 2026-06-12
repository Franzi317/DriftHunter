# Study 3 — Post-Earnings-Announcement Drift (PEAD), Filing-Date Anchored

**Date committed:** 2026-06-12 (BEFORE any Study 3 return statistic was computed)
**Status:** criteria frozen; results pending
**Verdict vocabulary:** SIGNAL / NO-SIGNAL (knowledge question; nothing is
built or traded on a SIGNAL without a separate deployment spec).

## Hypothesis

Post-earnings-announcement drift (Bernard & Thomas) is the other canonical
slow anomaly: stocks with strongly positive earnings surprises continue to
outperform for weeks after the announcement. This study tests the LONG side
of that claim, 2021–2026, anchored at the SEC filing date, under the same
engine and discipline as Phase 0 / Study 2. This is test #3 on adjacent data;
the bar stays at Study 2's stricter level.

## Data & definitions (frozen)

- **Source:** Sharadar SF1, dimension **ARQ** (as-reported quarterly),
  fields ticker, calendardate, datekey, epsdil, eps. EPS series = `epsdil`,
  falling back to `eps` where `epsdil` is null.
- **Anchor / trigger_date = `datekey`** (the SEC filing date of the
  quarterly report). Pre-registered caveat: datekey is AT OR AFTER the press
  release; this anchors at the latest unambiguous public moment. A NO-SIGNAL
  here does not rule out press-release-anchored drift; that variant is not
  testable with this data.
- **Surprise (SUE):** per ticker, seasonal random-walk surprise
  `d_q = eps_q − eps_{q−4}` on the ARQ series ordered by calendardate;
  `SUE_q = d_q / std(d_{q−1..q−8}, ddof=1)` requiring ≥6 non-null trailing
  seasonal diffs and std > 0. No analyst estimates are used (none exist in
  this data); this is the classical estimate-free SUE.
- **Event:** SUE ≥ **2.0** (absolute per-ticker threshold; no
  cross-sectional ranking, no lookahead), datekey within
  **2021-04-01 → 2026-03-31**. One event per (ticker, datekey).
  Long side only; shorting negative surprises is out of scope (stated
  limitation: the literature's short leg is untested here).
- **Universe filters at signal time:** ticker maps to an allowed exchange
  (existing TickerMap rules) and passes the derivative-listing screen.
  Entry-time tradability filters (price ≥ $2, ADV ≥ $1M) apply inside the
  engine as in all prior studies.
- **Event study:** existing `run_event_study`, horizons **[5, 20, 60]**
  trading days, costs [30] bp, benchmark SPY; IWM as labeled diagnostic
  (cannot affect the verdict). Entry at next open after datekey.

## Pre-committed SIGNAL criteria (ALL must hold at ≥1 horizon)

1. **n ≥ 500** completed events at that horizon.
2. **99% bootstrap CI lower bound > 0** on mean per-event excess return
   after 30bp vs SPY (alpha=0.01, seed 42, 10,000 iterations).
3. **Economic floors** on the mean: ≥ +0.5% (5d), ≥ +1.0% (20d),
   ≥ +1.5% (60d).
4. **Positive mean excess in ≥3 of 5 vintage calendar years** (by datekey).
5. **Price coverage ≥ 80%**, else SUSPECT- prefix.

## Pre-registered interpretations

- **NO-SIGNAL:** PEAD, as testable at SEC-filing-date latency with
  estimate-free SUE, joins the filing-drift family: real or not historically,
  it is not reachable by this design in this window. The third dead family;
  expected base case given Studies 1–2.
- **SIGNAL:** would justify (at most) a deployment-design phase with fresh
  gates — and would first demand a regime/subsample robustness pass, given
  the multiple-testing position of this study.
- Any SIGNAL with negative same-horizon IWM-relative mean must be flagged
  as size-regime exposure.

## Out of scope

Short leg; analyst-consensus surprises; press-release anchoring; intraday
entry; revenue/guidance surprises; sector exclusions; any change to the
Phase 0 / Study 2 artifacts.
