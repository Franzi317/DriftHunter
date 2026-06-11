# DriftHunter

An event-driven SEC-filing backtest that killed its own strategy — by design.

[![tests](https://github.com/Franzi317/DriftHunter/actions/workflows/tests.yml/badge.svg)](https://github.com/Franzi317/DriftHunter/actions/workflows/tests.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## The verdict, up front

**Phase 0 returned KILL on both signal profiles**, per the go/kill criteria
pre-committed *before any data was seen* — the thresholds live frozen in
[`config.yaml`](config.yaml)'s `gate:` block, and the
[runbook](docs/phase0-runbook.md) records them being applied unchanged.
No live trading system was built.

Headline numbers (30bp round-trip cost, vs SPY, 2021-04-01 → 2026-03-31):

**Form 4 cluster buys (n=4,651)**

| Horizon (days) | Mean excess | 95% CI | Pass |
|---|---|---|---|
| 5  | +0.04% | [-0.21%, +0.29%] | ❌ |
| 10 | -0.03% | [-0.38%, +0.32%] | ❌ |
| 20 | -0.50% | [-0.94%, -0.05%] | ❌ |
| 40 | -1.27% | [-1.94%, -0.59%] | ❌ |

**13D initiations (n=2,976)**

| Horizon (days) | Mean excess | 95% CI | Pass |
|---|---|---|---|
| 5  | -1.60% | [-2.18%, -0.98%] | ❌ |
| 10 | -2.17% | [-3.02%, -1.29%] | ❌ |
| 20 | -2.48% | [-4.05%, -0.74%] | ❌ |
| 40 | -7.31% | [-8.53%, -6.09%] | ❌ |

Both profiles fail the gate at every horizon. Full tables, exclusion reasons,
and per-horizon checks: [`data/report.md`](data/report.md). Run narrative,
data sources, and process notes: [`docs/phase0-runbook.md`](docs/phase0-runbook.md).

This is a **decisive** KILL, not a near-miss. Per the spec: "stop here
permanently."

---

## Why this repo exists

Most public trading-bot repos show curve-fit backtests with suspiciously
clean equity curves. This one shows the opposite: a fully-specified system
that was built, run honestly against five years of survivorship-bias-free
data, and **shut down by its own pre-committed rules** before a single dollar
was risked.

The process, in order:

1. Write the spec, including the exact go/kill criteria, **before** building
   anything (the criteria are frozen in [`config.yaml`](config.yaml)'s `gate:`
   block and were applied unchanged — see the [runbook](docs/phase0-runbook.md)).
2. Build the backtest engine (100 passing tests).
3. Run it once against 5 years of data.
4. Accept the verdict — KILL — without retuning, reslicing, or
   benchmark-shopping until it looked better.

### The predecessor lesson

This design exists because of a prior system (EdgeHunter) that reported a
**0.917 validation AUC** and then **lost money live** — a classic
train/serve skew, where the model that scored signals in backtesting wasn't
byte-identical to the one scoring them live.

DriftHunter makes that specific failure mode structurally impossible: the
scorer (`drifthunter/scorer/`) is **one module**, imported byte-identically
by the backtester and by any future live system. There is no separate
"research" version of the signal logic that could silently drift from
production.

---

## The interesting finding

The KILL verdict isn't "no edge exists" — it's "the edge exists, but this
system can't reach it at daily latency." See
[`data/diagnostics.md`](data/diagnostics.md) for the full post-mortem.

Between the **last pre-filing close** and the **next-open entry** (the
earliest fill this system's entry rule allows), prices already moved, on
average, SPY-adjusted:

| Profile | Metric | N | Mean | 95% CI |
|---|---|---|---|---|
| Form 4 clusters | `missed_total` (pre-close → next open) | 5,739 | +2.86% | [+2.69%, +3.04%] |
| 13D initiations | `missed_total` (pre-close → next open) | 4,641 | +1.67% | [+1.15%, +2.32%] |

That entire move happens **before** a next-open entrant can be filled. After
entry, the documented post-filing drift is essentially gone — mean excess
returns are zero-to-negative against SPY at every horizon (see table above),
and the same holds when the benchmark is swapped to IWM (small/mid-cap proxy):
form4 averages -0.11% excess vs IWM across horizons, sc13d averages -2.59%.
Switching benchmarks doesn't rescue either profile — this looks like a dead
signal at this latency, not a regime-headwind artifact.

In short: **the academic post-filing drift is real in this data, but it's
consumed before a daily-bar, next-open strategy can capture it.** Capturing
it would require same-day-of-filing (intraday) entry, which this system
deliberately does not attempt — intraday execution was an explicit
out-of-scope decision from day one.

---

## Architecture

```
ingest (EDGAR)  ->  scorer  ->  prices  ->  event study  ->  portfolio sim  ->  gate report
```

- **`drifthunter/ingest/`** — EDGAR-polite client: hard rate cap (8 req/s),
  exponential backoff on 429/5xx, disk cache so reruns are resumable and
  free. Pulls SEC Insider Transactions Data Sets (Form 4) and EDGAR
  form-index + filing headers (SC 13D).
- **`drifthunter/scorer/`** — deterministic, config-driven signal rules
  (Form 4 cluster buys, SC 13D initiations). This is the **single module**
  shared byte-identically between backtest and any future live system —
  see "Why this repo exists" above. Joint filers on the same Form 4 / 13D
  are collapsed into a single signal rather than double-counted.
- **`drifthunter/prices/`** — price provider abstraction. Primary: Sharadar
  SEP (paid, survivorship-bias-free — includes delisted tickers). Free
  fallback: yfinance/Stooq, with an automated coverage report (and an
  explicit <80% "suspect" flag) so any survivorship gap is auditable rather
  than silent.
- **`drifthunter/backtest/event_study.py`** — per-event excess return vs a
  benchmark (SPY) across 4 holding horizons (5/10/20/40 trading days) and a
  cost sweep (10/30/60bp), entering at the next open after the filing.
- **`drifthunter/backtest/portfolio.py`** — capital-constrained replay
  ($5k bankroll, max 5 concurrent positions, **all-or-nothing position
  sizing** — no partial fills), reporting annualized excess return, max
  drawdown, and capacity-miss rate.
- **`drifthunter/backtest/report.py`** — evaluates the pre-committed gate
  (spec §3.6) and renders `data/report.md`. The gate logic doesn't know or
  care whether the answer is GO or KILL.

**Key properties:**

- **Deterministic** — seeded bootstrap (seed pinned in `config.yaml`),
  same inputs + seed produce byte-identical reruns. Tested
  (`tests/test_cli_e2e.py::test_study_is_deterministic`).
- **Cost-first** — every result is reported net of round-trip costs; 30bp is
  the headline case the gate must pass at.
- **Survivorship-bias-aware** — paid Sharadar SEP includes delisted names;
  the free fallback path reports per-profile coverage and flags <80% as
  suspect.

---

## Run it yourself

```bash
uv sync
```

The pipeline is three commands, run in order:

```bash
uv run drifthunter signals   # ingest + score -> data/signals.parquet
uv run drifthunter study     # fetch prices + event study -> data/events.parquet, data/coverage.json
uv run drifthunter report    # evaluate the gate + render -> data/report.md
```

**Prices — two paths:**

- **Paid (used for the run in this repo):** set `prices.provider: sharadar`
  in `config.yaml` and export `NASDAQ_DATA_LINK_API_KEY`. Survivorship-bias-free,
  ~$40 for a one-month subscription — pull, run, cancel.
- **Free ($0):** set `prices.provider: free` in `config.yaml` (yfinance/Stooq).
  No delisted tickers, so check `data/coverage.json` — coverage below 80% is
  flagged as suspect in the report.

**Before running anything that talks to EDGAR**, replace the placeholder in
`config.yaml`:

```yaml
edgar:
  # SEC requires a real contact email in the User-Agent; replace before running.
  user_agent: "DriftHunter research your-email@example.com"
```

The SEC requires a real, working contact email in the `User-Agent` header for
all EDGAR requests — requests with a placeholder address may be rate-limited
or blocked.

```bash
uv run pytest tests/ -q   # 100 passed, network-free
```

---

## What's reusable

The engine pieces are intentionally generic and reusable for **other**
event-driven hypotheses:

- `drifthunter/backtest/event_study.py` — per-event excess-return study vs a
  benchmark, any horizon/cost sweep
- `drifthunter/backtest/report.py` — gate evaluator + markdown report
- `drifthunter/backtest/portfolio.py` — capital-constrained portfolio sim
- `drifthunter/ingest/` — EDGAR-polite client with caching

**If you reuse this for a new strategy idea: write your own spec with your
own pre-committed go/kill gates *before* looking at any returns.** The
discipline is the point — a gate written after you've seen the results isn't
a gate, it's a rationalization.

---

## Disclaimers

This repository is for research and educational purposes only. It is **not**
investment advice, and nothing here should be construed as a recommendation
to buy or sell any security. The strategies evaluated here returned negative
results and were killed; past performance — especially negative
past performance — is no guarantee of future results. The software is
provided with no warranty of any kind (see [LICENSE](LICENSE)). Use at your
own risk.
