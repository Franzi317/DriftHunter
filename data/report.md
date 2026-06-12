# DriftHunter Phase 0 Report

## Profile: form4 — **KILL**

Coverage: 88.7%
Reasons: CI lo > 0; portfolio excess > 0; max DD < 25%

| Horizon | N | Mean excess | 95% CI | Pos. years | Port. excess (ann.) | Max DD | Skipped (full book) | Pass |
|---|---|---|---|---|---|---|---|---|
| 5 | 4653 | +0.0003 | [-0.0022, +0.0028] | 3 | -14.95% | 49.88% | 718 | ❌ |
| 10 | 4653 | -0.0004 | [-0.0038, +0.0032] | 2 | -2.57% | 48.56% | 2312 | ❌ |
| 20 | 4653 | -0.0051 | [-0.0095, -0.0005] | 1 | -27.46% | 56.88% | 23 | ❌ |
| 40 | 4653 | -0.0128 | [-0.0194, -0.0058] | 2 | -17.74% | 54.51% | 90 | ❌ |

## Profile: sc13d — **KILL**

Coverage: 93.4%
Reasons: CI lo > 0; >=3 positive years; max DD < 25%

| Horizon | N | Mean excess | 95% CI | Pos. years | Port. excess (ann.) | Max DD | Skipped (full book) | Pass |
|---|---|---|---|---|---|---|---|---|
| 5 | 4256 | -0.0187 | [-0.0236, -0.0137] | 1 | -40.32% | 89.47% | 332 | ❌ |
| 10 | 4256 | -0.0260 | [-0.0327, -0.0189] | 1 | -14.88% | 72.92% | 2288 | ❌ |
| 20 | 4256 | -0.0307 | [-0.0423, -0.0175] | 1 | +9.66% | 48.15% | 3086 | ❌ |
| 40 | 4256 | -0.0744 | [-0.0847, -0.0637] | 0 | -23.83% | 71.19% | 681 | ❌ |

> Max DD is computed on the REALIZED equity curve (positions held at cost
> until exit); it understates true intra-position peak-to-trough drawdown.
> Treat the gate's drawdown check as a lower bound, not an estimate.

> The bootstrap CI resamples per-event excess returns independently. Events with
> overlapping holding windows share market-regime exposure, so the true CI is wider
> than reported; treat "CI lo > 0" as necessary but not sufficient, and prefer a
> comfortable margin over a marginal pass.

## Excluded events by reason

- adv: 1704 events (898 tickers)
- insufficient_history: 93 events (46 tickers)
- min_price: 503 events (297 tickers)
- no_prices: 818 events (384 tickers)