# DriftHunter Phase 0 Report

## Profile: form4 — **KILL**

Coverage: 88.7%
Reasons: CI lo > 0; portfolio excess > 0; max DD < 25%

| Horizon | N | Mean excess | 95% CI | Pos. years | Port. excess (ann.) | Max DD | Skipped (full book) | Pass |
|---|---|---|---|---|---|---|---|---|
| 5 | 4651 | +0.0004 | [-0.0021, +0.0029] | 3 | -14.95% | 49.88% | 718 | ❌ |
| 10 | 4651 | -0.0003 | [-0.0038, +0.0032] | 2 | -2.57% | 48.56% | 2312 | ❌ |
| 20 | 4651 | -0.0050 | [-0.0094, -0.0005] | 1 | -27.46% | 56.88% | 23 | ❌ |
| 40 | 4651 | -0.0127 | [-0.0194, -0.0059] | 2 | -17.74% | 54.51% | 90 | ❌ |

## Profile: sc13d — **KILL**

Coverage: 92.3%
Reasons: CI lo > 0; >=3 positive years; max DD < 25%

| Horizon | N | Mean excess | 95% CI | Pos. years | Port. excess (ann.) | Max DD | Skipped (full book) | Pass |
|---|---|---|---|---|---|---|---|---|
| 5 | 2976 | -0.0160 | [-0.0218, -0.0098] | 1 | -28.82% | 77.02% | 332 | ❌ |
| 10 | 2976 | -0.0217 | [-0.0302, -0.0129] | 1 | +0.99% | 52.52% | 1680 | ❌ |
| 20 | 2976 | -0.0248 | [-0.0405, -0.0074] | 1 | +36.53% | 25.89% | 1970 | ❌ |
| 40 | 2976 | -0.0731 | [-0.0853, -0.0609] | 0 | -21.73% | 57.31% | 681 | ❌ |

> Max DD is computed on the REALIZED equity curve (positions held at cost
> until exit); it understates true intra-position peak-to-trough drawdown.
> Treat the gate's drawdown check as a lower bound, not an estimate.

> The bootstrap CI resamples per-event excess returns independently. Events with
> overlapping holding windows share market-regime exposure, so the true CI is wider
> than reported; treat "CI lo > 0" as necessary but not sufficient, and prefer a
> comfortable margin over a marginal pass.

## Excluded events by reason

- adv: 1459 events (776 tickers)
- insufficient_history: 74 events (36 tickers)
- min_price: 361 events (207 tickers)
- no_prices: 792 events (370 tickers)