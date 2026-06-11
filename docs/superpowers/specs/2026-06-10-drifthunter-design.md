# DriftHunter — Event-Driven SEC Filing Trading System

**Date:** 2026-06-10
**Status:** Design approved; pending implementation plan
**Predecessors:** edgar-signal (signal architecture), EdgeHunter (risk-engine philosophy, archived), CarryHunter (gate discipline, NO-GO), StrikePoint (execution lessons)

---

## 1. Purpose & thesis

Capture the documented post-filing drift after two SEC filing events with decades
of academic support:

1. **Form 4 insider cluster buys** — open-market purchases by multiple insiders
   (or large purchases by CEO/CFO) predict positive excess returns over the
   following days to weeks, concentrated in small/mid caps where limits to
   arbitrage keep the anomaly alive.
2. **13D initiations** — new activist >5% positions show well-documented
   announcement drift.

Both edges decay over **days to weeks**, so a retail system polling EDGAR every
60 seconds is fast enough. This is the decisive difference from the two failed
predecessors: EdgeHunter competed on 15-minute crypto direction (near-efficient,
fee-dominated) and CarryHunter's funding carry was real but thinner than
Treasuries. Here the edge is slow, documented, and structurally protected by
small-cap illiquidity at institutional size — while being perfectly liquid at
our size.

**Capital:** under $5,000. **Consequence:** recurring costs must be ~$0
(free live data, LLM only for alert prose; the single exception is a ~$40
one-month subscription to a survivorship-bias-free price dataset for the
Phase 0 backtest — see §3.2), engineering stays lean, and expected profit is
honestly modest — this is an edge-validation system that scales if it works,
not a get-rich machine.

## 2. Non-negotiable design principles

1. **One scorer, two consumers.** The deterministic scoring module is imported
   byte-identically by the backtester and the live system. No train/serve skew —
   EdgeHunter's fatal flaw (0.917 validation AUC, −$94.50 realized over ~16,700
   trades) is structurally impossible.
2. **Deterministic decision path.** No LLM, no ML model gates any trade. LLMs
   may write human-readable Discord alert text only, strictly outside the
   decision path; an LLM failure can never block or trigger a trade.
3. **Pre-committed gates with kill branches.** Every phase has go/kill criteria
   written down before the phase runs (CarryHunter's discipline — it worked).
   A failed gate means stop-and-diagnose with a written reason before re-entry,
   never tune-until-green.
4. **Risk engine has absolute veto.** Deterministic hard limits checked before
   every order; nothing overrides them.
5. **Costs are first-class in validation.** Spread/slippage killed both failed
   predecessors. The backtest sweeps cost assumptions and the conservative case
   must still pass.
6. **EDGAR etiquette:** ≤8 req/sec hard cap, exponential backoff on 429/5xx,
   User-Agent with real contact email, dedup by accession number before any work.

## 3. Phase 0 — Backtest gate (build this first, build nothing else)

### 3.1 Data sources (all free)

| Data | Source | Notes |
|---|---|---|
| Form 4 history (2006→) | SEC **Insider Transactions Data Sets** (quarterly TSV bundles) | Already structured: submission, non-derivative transaction, reporting-owner tables. No scraping. |
| SC 13D history | EDGAR daily/quarterly **form index** files + filing **headers** | Header parse only (subject company, filer, date, form type). Document text not needed for deterministic rules. |
| CIK → ticker | SEC `company_tickers.json` | Plus exchange listing for OTC exclusion. |
| Prices (Phase 0) | **Paid survivorship-bias-free daily OHLCV** (Sharadar or Norgate class, ~$40 for one month); SPY as benchmark | Includes delisted tickers. Subscribe for one month, pull event windows, run study, cancel. yfinance + Stooq are the free fallback if the paid source is unavailable. |
| Prices (live) | Alpaca market data for quotes/fills; yfinance for outcome tracking | Survivorship bias does not apply to positions actually held. |

### 3.2 Survivorship bias — first-class treatment

Free price sources (yfinance) lack delisted tickers, and small caps are where
delistings happen — a silent bias of exactly the kind that produced
EdgeHunter's fake validation AUC. **Primary mitigation: use a paid
survivorship-bias-free dataset for Phase 0** (one month, ~$40; it includes
delisted tickers, removing the bias rather than merely measuring it). Budget
~$40/year thereafter only if re-validating: extending the backtest window,
changing scorer rules, or investigating a drift alarm each require a fresh
one-month pull. The live system never needs paid data.

Regardless of source, the backtester must:

- Report **price-coverage rate** per profile (events with usable prices ÷ total
  events).
- Flag results as suspect if coverage < 80% (relevant mainly on the free
  fallback path; expect ~100% on the paid path).
- List every uncovered event so the gap is auditable.
- State any residual bias explicitly in the final report.

### 3.3 Scorer rules (deterministic, config-driven thresholds)

**Form 4 cluster-buy profile.** Eligible transaction: non-derivative,
transaction code P (open-market purchase), value (shares × price) ≥ $25k.
Signal fires for an issuer when any of:

- ≥2 distinct insiders with eligible buys within a trailing 10-trading-day window
- single eligible buy ≥ $250k by CEO or CFO
- any single eligible buy ≥ $1M

Score increases with cluster size, total dollar value, and insider seniority.
Known limitation: 10b5-1 plan flagging is unreliable in the structured dataset
before the Dec 2023 checkbox; accepted and documented.

**13D initiation profile.** New SC 13D (amendments excluded) where the subject
company has a listed common-stock ticker passing the tradability filters below.
Score boost if the filer CIK is on a small curated activist watchlist
(maintained in config).

**Tradability filters (both profiles):** listed exchange (no OTC), price ≥ $2,
average daily dollar volume ≥ $1M.

### 3.4 Event study

- **Entry:** next market open after filing acceptance timestamp.
- **Exit sweep:** 5 / 10 / 20 / 40 trading days, exit at open.
- **Metric:** per-event excess return vs SPY over the same window.
- **Costs:** round-trip cost swept 10–60bp; 30bp is the headline case. Gate
  must pass at 30bp.
- **Window:** most recent ~5 calendar years.

### 3.5 Portfolio simulation

Capital-constrained replay: $5k bankroll, max 5 concurrent positions,
equal-weight ~$1k each, max 2 entries/day, skip signals when full (no bumping).
Reports net annualized excess return, max drawdown, turnover, capacity-miss rate
(signals skipped because the book was full).

### 3.6 Phase 0 go/kill criteria (pre-committed)

**GO** if at least one profile shows ALL of:

- Sample: ≥300 price-covered events (Form 4) or ≥150 (13D)
- Mean per-event excess return after 30bp costs > 0, bootstrap 95% CI excludes 0
- Positive mean excess return in ≥3 of 5 calendar years
- Portfolio sim: positive net annualized excess return, max drawdown < 25%

**KILL** if neither profile passes: write the report, stop. The backtester
remains as a reusable research tool; no live system is built.

If only one profile passes, build the live system for that profile only.

## 4. Live system (built only on GO)

Single Python process on the user's Linux box, systemd service with
restart-on-failure. SQLite + Alembic for state; structlog JSON logs.

```
EDGAR Poller (60s, market days; current-filings feed)
  → Parser (Form 4 XML → transactions; 13D header → subject/filer)
  → Scorer (THE Phase-0-validated module, imported verbatim)
  → Decision Engine (signal → trade intent)  ⇄  Risk Engine (veto)
  → Executor (Alpaca; paper/live config switch)
  → Exit Scheduler · Outcome Tracker · Discord alerts (hermes-gateway)
```

### 4.1 Components

- **Poller** — EDGAR current-filings feed for Form 4 + SC 13D every 60s during
  market days. Rate cap, backoff, dedup, idempotent across restarts. (Live
  ingestion differs from Phase 0 bulk ingestion by necessity; the *scorer* is
  the shared component, and §5 testing enforces path equivalence.)
- **Parser** — Form 4 XML and 13D header parsing with golden-fixture tests.
- **Decision engine** — long-only, equal-weight ~$1k intents. **Entry timing
  matches the backtest assumption:** intents are queued and executed at the
  next market open after the filing's acceptance timestamp, not immediately on
  detection. Full book ⇒ skip signal (logged with reason); open positions are
  never bumped.
- **Executor** — Alpaca, stocks only. Entry: limit at mid, 2-minute timeout,
  then marketable limit fallback; never blind market orders. Poll until
  filled/rejected; record actual fill price as entry; confirm closes against
  broker; reconcile DB vs Alpaca holdings on every startup and alert on
  mismatch. (Every StrikePoint Phase 3A failure becomes a day-one requirement.)
- **Exit scheduler** — time-based exit at the Phase-0-selected horizon. No
  stop-losses unless Phase 0 shows they improve net results.
- **Outcome tracker** — nightly: realized return per closed position vs SPY,
  compared against the backtest expectation band. **Drift alarm:** if 20+
  consecutive closed positions fall outside the backtest CI band, alert and
  recommend halt.
- **Alerts** — entry/exit/halt/digest to Discord via hermes-gateway. Optional
  Haiku call composes alert prose; on failure, send a plain templated message.

### 4.2 Risk engine (hard limits, veto-only)

- Max 5 concurrent positions; max $1.2k notional per position; long-only; no margin
- Max 2 new entries per day
- 1 position per ticker; 10-trading-day re-entry cooldown after exit
- Drawdown halt: −15% from high-water mark ⇒ no new entries, alert, manual reset required
- Kill switch: `~/.drifthunter/KILL_SWITCH` file checked before every order
- Market open + ticker tradeable/halted check via Alpaca clock/asset API before any order
- Stale-data guard: no quote fresher than 10 minutes ⇒ no trade

## 5. Testing

- **Parser golden tests:** real Form 4 XML + 13D header fixtures (public data,
  committed) with exact expected outputs.
- **Scorer unit tests:** synthetic filings covering every rule boundary
  (cluster window edges, role thresholds, value thresholds, OTC exclusion).
- **Backtest determinism:** same inputs + seed ⇒ byte-identical results.
- **Backtest↔live consistency test (the no-skew guarantee, in CI forever):**
  one historical filing fed through the live parser→scorer path and the
  backtest path must produce identical signals.
- **Executor tests:** mocked Alpaca — partial fills, rejections,
  timeout-then-fill, startup reconciliation mismatches.
- **Risk engine tests:** every limit at its boundary; kill-switch behavior.

## 6. Phase gates

| Phase | What runs | Advance gate |
|---|---|---|
| 0 Backtest | Historical event study | §3.6 criteria |
| 1 Paper | Full system on Alpaca paper, ~8 weeks | ≥15 completed round trips; zero unreconciled positions or unverified fills; realized returns inside backtest CI band; fills within modeled slippage |
| 2 Live micro | Real money, $1.5k, ~8 weeks | Same checks at real fill quality; zero operational incidents |
| 3 Full size | $5k | Ongoing: drift alarm quiet; gates re-checked quarterly |

Failed gate ⇒ stop and diagnose; re-entry requires a written statement of what
changed and why. Never tune-until-green.

## 7. Operations

- systemd unit, restart-on-failure; nightly SQLite backup
- Weekly Discord digest: positions, P&L, signal counts, skipped signals with
  reasons, EDGAR poll health, price-feed health
- Recurring cost target: $0 live data, pennies/month LLM; ~$40 one-time for
  Phase 0 price data (and ~$40 per re-validation, expected ≤1/year)

## 8. Out of scope (YAGNI)

Options, shorting, 8-K/13F profiles, ML models, intraday entry timing,
multi-broker support, Hermes/agent self-modification of trading logic
(the outcome tracker reports; humans change code), web dashboard (Discord
digest suffices at this scale).

## 9. What was salvaged vs redesigned

| From | Salvaged | Deliberately not salvaged |
|---|---|---|
| edgar-signal | Poller discipline (rate/backoff/dedup/UA), fixture-driven testing, outcome-tracking-as-first-class, SQLite+Alembic+structlog stack | LLM stages in the decision path; live-oriented schema |
| EdgeHunter | Deterministic risk-engine philosophy, paper/live config split | Everything predictive (XGBoost, EV gating) |
| CarryHunter | Pre-committed gate discipline, honest cost/bias accounting | The strategy (NO-GO stands) |
| StrikePoint | Execution lessons as requirements (fill verification, reconciliation, actual fill price) | Options trading, TA signal engine, yfinance-driven decisions, self-tuning loop |
