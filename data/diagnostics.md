# DriftHunter Phase 0 Post-Mortem Diagnostics

**Purpose**: explain WHY the Phase 0 gate returned KILL for both profiles (see `data/report.md`). This document does not change any engine code, gate logic, or the existing report.

> **Post-mortem diagnostic only -- the Phase 0 verdict (KILL vs SPY per spec Sec3.6) is unaffected.**

Signals analyzed: 13683 (6439 form4, 7244 sc13d). Global price span: 2021-01-31 -> 2026-09-17.

```
SANITY CHECK (Diagnostic-A price logic vs data/events.parquet):
  ticker=DSGN trigger_date=2021-04-01 horizon=5
  events.parquet entry_date=2021-04-05 raw_return=-0.099700
  recomputed       entry_date=2021-04-05 entry_open=29.9900 raw_return=-0.099700
  entry_date match: True; raw_return match: True
```

## Diagnostic A: the missed day-0 pop (form4 + sc13d separately)

Question: did the documented filing reaction happen BEFORE our entry (next open after `trigger_date`)?

- `day0_return`: for signals where `trigger_date` itself was a trading day, `close[trigger] / close[pre] - 1` -- the filing-day move we could never capture.
- `missed_total`: `open[entry] / close[pre] - 1` -- everything between the last pre-filing close and our actual entry price (the full "pop we missed").
- `*_excess` variants subtract SPY's return over the identical date pairs.
- Skipped signals: 1006 (no price data), 0 (no row after trigger_date), 157 (no row before trigger_date).

### Profile: form4

| Metric | N | Mean | 95% CI lo | 95% CI hi |
|---|---|---|---|---|
| day0_return (raw) | 5738 | +0.0083 | +0.0068 | +0.0098 |
| day0_return (SPY-adjusted) | 5738 | +0.0079 | +0.0065 | +0.0094 |
| missed_total (raw) | 5741 | +0.0293 | +0.0276 | +0.0311 |
| missed_total (SPY-adjusted) | 5741 | +0.0286 | +0.0269 | +0.0304 |

### Profile: sc13d

| Metric | N | Mean | 95% CI lo | 95% CI hi |
|---|---|---|---|---|
| day0_return (raw) | 6765 | +0.0063 | +0.0031 | +0.0102 |
| day0_return (SPY-adjusted) | 6765 | +0.0054 | +0.0023 | +0.0092 |
| missed_total (raw) | 6779 | +0.0172 | +0.0128 | +0.0221 |
| missed_total (SPY-adjusted) | 6779 | +0.0160 | +0.0116 | +0.0209 |

### Interpretation

Rule: if `missed_total` excess is significantly POSITIVE while the event study's post-entry excess was ~0/negative, the reaction concentrates before entry (timing problem); if `missed_total` is also ~0, the signal simply has no reaction at daily granularity (dead signal).

**form4**: missed_total excess is significantly POSITIVE (mean +0.0286, 95% CI [+0.0269, +0.0304]), consistent with the documented filing reaction concentrating BEFORE our entry -- a timing problem rather than a dead signal. On the subset of signals where trigger_date itself was a trading day, the same-day excess move (day0_return_excess) has mean +0.0079, 95% CI [+0.0065, +0.0094].

**sc13d**: missed_total excess is significantly POSITIVE (mean +0.0160, 95% CI [+0.0116, +0.0209]), consistent with the documented filing reaction concentrating BEFORE our entry -- a timing problem rather than a dead signal. On the subset of signals where trigger_date itself was a trading day, the same-day excess move (day0_return_excess) has mean +0.0054, 95% CI [+0.0023, +0.0092].

## Diagnostic B: IWM benchmark decomposition

The Phase 0 event study re-run with `benchmark="IWM"` (Russell 2000 ETF, a small/mid-cap proxy) instead of `benchmark="SPY"`, compared side-by-side with the SPY numbers already recorded in `data/events.parquet` (same slice: `cost_bps == 30`, `filter_reason == ""`).

### Profile: form4 (cost_bps=30)

| Horizon | N (SPY) | Mean excess vs SPY | 95% CI vs SPY | N (IWM) | Mean excess vs IWM | 95% CI vs IWM |
|---|---|---|---|---|---|---|
| 5 | 4653 | +0.0003 | [-0.0022, +0.0028] | 4653 | +0.0018 | [-0.0007, +0.0042] |
| 10 | 4653 | -0.0004 | [-0.0038, +0.0032] | 4653 | +0.0016 | [-0.0018, +0.0052] |
| 20 | 4653 | -0.0051 | [-0.0095, -0.0005] | 4653 | -0.0013 | [-0.0057, +0.0032] |
| 40 | 4653 | -0.0128 | [-0.0194, -0.0058] | 4653 | -0.0068 | [-0.0132, -0.0000] |

### Profile: sc13d (cost_bps=30)

| Horizon | N (SPY) | Mean excess vs SPY | 95% CI vs SPY | N (IWM) | Mean excess vs IWM | 95% CI vs IWM |
|---|---|---|---|---|---|---|
| 5 | 4256 | -0.0187 | [-0.0236, -0.0137] | 4256 | -0.0171 | [-0.0220, -0.0122] |
| 10 | 4256 | -0.0260 | [-0.0327, -0.0189] | 4256 | -0.0233 | [-0.0298, -0.0163] |
| 20 | 4256 | -0.0307 | [-0.0423, -0.0175] | 4256 | -0.0247 | [-0.0364, -0.0116] |
| 40 | 4256 | -0.0744 | [-0.0847, -0.0637] | 4256 | -0.0645 | [-0.0747, -0.0539] |

### Interpretation

**form4**: excess returns vs IWM remain non-positive on average (mean across horizons -0.0012 vs -0.0045 vs SPY), so switching the benchmark from SPY to a small/mid-cap index does not turn the result positive -- the underlying signal itself does not appear to beat its peer-universe drift ('signal dead' rather than purely a 'regime headwind' explanation).

**sc13d**: excess returns vs IWM remain non-positive on average (mean across horizons -0.0324 vs -0.0375 vs SPY), so switching the benchmark from SPY to a small/mid-cap index does not turn the result positive -- the underlying signal itself does not appear to beat its peer-universe drift ('signal dead' rather than purely a 'regime headwind' explanation).

## What this means for future work

- **form4**: `missed_total` excess CI is entirely positive ([+0.0269, +0.0304], n=5741). The data supports that, on average, price moves up between the last pre-filing close and our entry -- any future redesign aiming to capture this would need a same-day-of-filing entry mechanism (not available with daily-bar data and a next-open entry rule), or intraday data.
- **sc13d**: `missed_total` excess CI is entirely positive ([+0.0116, +0.0209], n=6779). The data supports that, on average, price moves up between the last pre-filing close and our entry -- any future redesign aiming to capture this would need a same-day-of-filing entry mechanism (not available with daily-bar data and a next-open entry rule), or intraday data.
- **form4**: average IWM-relative excess across horizons is -0.0012 (still <= 0), so benchmark choice (SPY vs a small/mid-cap index) does not by itself explain the KILL verdict -- the data does not support a 'pure regime headwind' story for this profile.
- **sc13d**: average IWM-relative excess across horizons is -0.0324 (still <= 0), so benchmark choice (SPY vs a small/mid-cap index) does not by itself explain the KILL verdict -- the data does not support a 'pure regime headwind' story for this profile.
