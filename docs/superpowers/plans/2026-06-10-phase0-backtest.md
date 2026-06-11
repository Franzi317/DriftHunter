# DriftHunter Phase 0 Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 0 backtest gate from the approved spec (`docs/superpowers/specs/2026-06-10-drifthunter-design.md` §3): ingest 5 years of Form 4 and SC 13D filings, score them with the deterministic shared scorer, run a cost-swept event study plus capital-constrained portfolio simulation, and emit a GO/KILL report against the pre-committed §3.6 criteria.

**Architecture:** A batch pipeline: `ingest → signals → prices → event study → portfolio sim → report`, with parquet files as the interface between stages and a `scorer/` package that takes plain dataclasses so the future live system can import it unchanged. No database, no daemon — Phase 0 is a research tool.

**Tech Stack:** Python 3.12, uv, pandas + pyarrow, httpx, click, PyYAML, numpy, pytest. Price providers: Sharadar SEP via `nasdaq-data-link` (primary), yfinance + Stooq CSV (free fallback).

**Scope guard:** This plan implements spec §3 only. The live system (spec §4) is gated on this plan's output and gets its own plan after a GO verdict. Do not build pollers, executors, or risk engines here.

---

## File structure

```
DriftHunter/
├── pyproject.toml
├── config.yaml                     # all thresholds; no hardcoded values in code
├── drifthunter/
│   ├── __init__.py
│   ├── config.py                   # YAML → typed dataclasses
│   ├── scorer/                     # SHARED MODULE — live system imports this later
│   │   ├── __init__.py
│   │   ├── models.py               # InsiderBuy, ThirteenDFiling, Signal
│   │   ├── form4.py                # cluster-buy rules
│   │   └── sc13d.py                # 13D initiation rules
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── http.py                 # shared rate-limited downloader (EDGAR etiquette)
│   │   ├── tickers.py              # CIK → ticker/exchange map
│   │   ├── insider_datasets.py     # SEC quarterly TSV bundles → list[InsiderBuy]
│   │   ├── form_index.py           # EDGAR full-index → SC 13D accession list
│   │   └── header_parse.py         # filing header → subject company / filer
│   ├── prices/
│   │   ├── __init__.py
│   │   ├── provider.py             # PriceProvider protocol, parquet cache, coverage
│   │   ├── sharadar.py             # paid primary
│   │   └── free.py                 # yfinance → Stooq fallback
│   ├── backtest/
│   │   ├── __init__.py
│   │   ├── event_study.py          # per-event excess returns, horizon × cost sweep
│   │   ├── stats.py                # bootstrap CI, yearly breakdown
│   │   ├── portfolio.py            # capital-constrained replay
│   │   └── report.py               # GO/KILL verdict + markdown report
│   └── cli.py                      # click commands
└── tests/
    ├── conftest.py
    ├── fixtures/                   # tiny TSV slices, real header snippets, synthetic prices
    └── test_*.py                   # one test file per module
```

---

### Task 1: Project skeleton + config loader

**Files:**
- Create: `pyproject.toml`, `config.yaml`, `drifthunter/__init__.py`, `drifthunter/config.py`, all package `__init__.py` files
- Test: `tests/test_config.py`

- [ ] **Step 1: Create pyproject and package skeleton**

`pyproject.toml`:

```toml
[project]
name = "drifthunter"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pandas>=2.2",
    "pyarrow>=16",
    "numpy>=1.26",
    "httpx>=0.27",
    "click>=8.1",
    "pyyaml>=6.0",
    "yfinance>=0.2.40",
    "nasdaq-data-link>=1.0.4",
]

[project.scripts]
drifthunter = "drifthunter.cli:cli"

[dependency-groups]
dev = ["pytest>=8.0", "pytest-httpx>=0.30"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["drifthunter"]
```

Create empty `__init__.py` in `drifthunter/`, `drifthunter/scorer/`, `drifthunter/ingest/`, `drifthunter/prices/`, `drifthunter/backtest/`, `tests/`.

`config.yaml` (every threshold from spec §3, nothing hardcoded in code):

```yaml
data_dir: data

form4:
  min_transaction_value: 25000        # spec §3.3: eligible buy >= $25k
  cluster_window_bdays: 10            # spec §3.3: 10 trading days (weekday approximation)
  cluster_min_insiders: 2
  ceo_cfo_single_min: 250000
  any_single_min: 1000000
  ceo_cfo_title_keywords: ["chief executive", "ceo", "chief financial", "cfo"]

sc13d:
  activist_ciks: []                   # curated list; score boost when filer matches

tradability:                          # spec §3.3 filters
  min_price: 2.0
  min_avg_dollar_volume: 1000000
  adv_window: 20
  allowed_exchanges: ["NYSE", "Nasdaq", "NYSE American", "NYSE Arca", "CBOE"]

study:                                # spec §3.4
  start: "2021-04-01"
  end: "2026-03-31"
  horizons: [5, 10, 20, 40]
  cost_bps_sweep: [10, 30, 60]
  headline_cost_bps: 30
  bootstrap_iterations: 10000
  seed: 42
  benchmark: SPY

portfolio:                            # spec §3.5
  bankroll: 5000.0
  position_size: 1000.0
  max_positions: 5
  max_entries_per_day: 2

gate:                                 # spec §3.6 — pre-committed, do not tune
  form4_min_events: 300
  sc13d_min_events: 150
  min_years_positive: 3
  total_years: 5
  max_drawdown: 0.25
  min_coverage: 0.80

prices:
  provider: sharadar                  # sharadar | free
  nasdaq_api_key_env: NASDAQ_DATA_LINK_API_KEY

edgar:
  user_agent: "DriftHunter research your-email@example.com"
  max_requests_per_sec: 8
```

- [ ] **Step 2: Write the failing config test**

`tests/test_config.py`:

```python
from pathlib import Path

from drifthunter.config import load_config


def test_load_config_reads_repo_yaml():
    cfg = load_config(Path(__file__).parents[1] / "config.yaml")
    assert cfg.form4.min_transaction_value == 25000
    assert cfg.form4.cluster_window_bdays == 10
    assert cfg.study.horizons == [5, 10, 20, 40]
    assert cfg.study.headline_cost_bps == 30
    assert cfg.gate.form4_min_events == 300
    assert cfg.tradability.min_price == 2.0
    assert cfg.portfolio.max_positions == 5
    assert "drifthunter" in cfg.edgar.user_agent.lower()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError` / `ImportError` for `drifthunter.config`

- [ ] **Step 4: Implement config loader**

`drifthunter/config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Form4Config:
    min_transaction_value: float
    cluster_window_bdays: int
    cluster_min_insiders: int
    ceo_cfo_single_min: float
    any_single_min: float
    ceo_cfo_title_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Sc13dConfig:
    activist_ciks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TradabilityConfig:
    min_price: float
    min_avg_dollar_volume: float
    adv_window: int
    allowed_exchanges: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StudyConfig:
    start: str
    end: str
    horizons: list[int]
    cost_bps_sweep: list[int]
    headline_cost_bps: int
    bootstrap_iterations: int
    seed: int
    benchmark: str


@dataclass(frozen=True)
class PortfolioConfig:
    bankroll: float
    position_size: float
    max_positions: int
    max_entries_per_day: int


@dataclass(frozen=True)
class GateConfig:
    form4_min_events: int
    sc13d_min_events: int
    min_years_positive: int
    total_years: int
    max_drawdown: float
    min_coverage: float


@dataclass(frozen=True)
class PricesConfig:
    provider: str
    nasdaq_api_key_env: str


@dataclass(frozen=True)
class EdgarConfig:
    user_agent: str
    max_requests_per_sec: int


@dataclass(frozen=True)
class Config:
    data_dir: Path
    form4: Form4Config
    sc13d: Sc13dConfig
    tradability: TradabilityConfig
    study: StudyConfig
    portfolio: PortfolioConfig
    gate: GateConfig
    prices: PricesConfig
    edgar: EdgarConfig


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    return Config(
        data_dir=Path(raw["data_dir"]),
        form4=Form4Config(**raw["form4"]),
        sc13d=Sc13dConfig(**raw["sc13d"]),
        tradability=TradabilityConfig(**raw["tradability"]),
        study=StudyConfig(**raw["study"]),
        portfolio=PortfolioConfig(**raw["portfolio"]),
        gate=GateConfig(**raw["gate"]),
        prices=PricesConfig(**raw["prices"]),
        edgar=EdgarConfig(**raw["edgar"]),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml config.yaml drifthunter tests
git commit -m "feat: project skeleton and typed config loader"
```

---

### Task 2: Scorer data models

**Files:**
- Create: `drifthunter/scorer/models.py`
- Test: `tests/test_scorer_models.py`

These dataclasses are the contract between ingestion (backtest: TSV; live system later: XML) and the scorer. Keep them free of pandas.

- [ ] **Step 1: Write the failing test**

`tests/test_scorer_models.py`:

```python
from datetime import date

from drifthunter.scorer.models import InsiderBuy, Signal, ThirteenDFiling


def test_insider_buy_value():
    buy = InsiderBuy(
        accession="0001-24-000001",
        issuer_cik="0000320193",
        ticker="AAPL",
        insider_cik="0001214156",
        insider_name="DOE JANE",
        is_ceo_cfo=True,
        trans_date=date(2024, 3, 1),
        filing_date=date(2024, 3, 4),
        shares=1000.0,
        price=30.0,
    )
    assert buy.value == 30000.0


def test_thirteen_d_amendment_flag():
    f = ThirteenDFiling(
        accession="0002-24-000002",
        subject_cik="0000789019",
        subject_name="EXAMPLECO",
        ticker="EXM",
        filer_cik="0001336528",
        filer_name="ACTIVIST LP",
        filing_date=date(2024, 5, 6),
        is_amendment=True,
    )
    assert f.is_amendment


def test_signal_fields():
    s = Signal(profile="form4", ticker="EXM", trigger_date=date(2024, 5, 6),
               score=3.2, detail="cluster=2 total=$120,000")
    assert s.profile == "form4"
    assert s.score > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scorer_models.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement models**

`drifthunter/scorer/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class InsiderBuy:
    """One non-derivative open-market purchase (Form 4, code P)."""
    accession: str
    issuer_cik: str
    ticker: str | None
    insider_cik: str
    insider_name: str
    is_ceo_cfo: bool
    trans_date: date
    filing_date: date
    shares: float
    price: float

    @property
    def value(self) -> float:
        return self.shares * self.price


@dataclass(frozen=True)
class ThirteenDFiling:
    """One SC 13D (or 13D/A) filing, header-level data only."""
    accession: str
    subject_cik: str
    subject_name: str
    ticker: str | None
    filer_cik: str
    filer_name: str
    filing_date: date
    is_amendment: bool


@dataclass(frozen=True)
class Signal:
    """A scored trade candidate. profile is 'form4' or 'sc13d'."""
    profile: str
    ticker: str
    trigger_date: date
    score: float
    detail: str
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scorer_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add drifthunter/scorer/models.py tests/test_scorer_models.py
git commit -m "feat: scorer dataclass models (InsiderBuy, ThirteenDFiling, Signal)"
```

---

### Task 3: Form 4 cluster-buy scorer

**Files:**
- Create: `drifthunter/scorer/form4.py`
- Test: `tests/test_scorer_form4.py`

> **AMENDED during execution (2026-06-10):** the original inline implementation and the
> `test_score_increases_with_cluster_size_and_ceo` test below were inconsistent (the CEO
> buy arrived after the fire date, so no causal semantics could include it). The
> implemented semantics are **day-batched eager firing** (commit `2a3fa9d`), which is
> canonical; the code blocks below are kept for history.

Rules from spec §3.3, as implemented (day-batched eager fire):
- Per issuer, eligible buys (value ≥ `min_transaction_value`, ticker present) are grouped
  by `filing_date`; filing dates are walked in ascending order.
- At date D the window = eligible buys with `0 <= busday_count(filing_date, D) <= cluster_window_bdays`
  (weekday approximation; holidays ignored deliberately — documented in code).
- Fires at D when the window holds ≥`cluster_min_insiders` distinct insiders, OR any single
  buy in the window is CEO/CFO with value ≥ `ceo_cfo_single_min`, OR any single buy in the
  window has value ≥ `any_single_min`.
- Same-day filings batch into one evaluation (live entry queues to next open anyway).
- After a fire, the issuer is suppressed until `cluster_window_bdays` business days pass.

Score = `distinct_insiders + log10(total_window_value / min_transaction_value) + 1.0 if any CEO/CFO in window`. The replacement tests are `test_score_increases_with_ceo_in_cluster_at_fire_time` (both signals fire same day; big has the CEO at fire time) and `test_same_day_buys_batch_into_one_signal` (3 insiders incl. 2 same-day → one signal, `insiders=3`, score > 4.0).

- [ ] **Step 1: Write the failing tests**

`tests/test_scorer_form4.py`:

```python
from datetime import date

from drifthunter.config import Form4Config
from drifthunter.scorer.form4 import detect_signals
from drifthunter.scorer.models import InsiderBuy

CFG = Form4Config(
    min_transaction_value=25000,
    cluster_window_bdays=10,
    cluster_min_insiders=2,
    ceo_cfo_single_min=250000,
    any_single_min=1000000,
    ceo_cfo_title_keywords=["ceo", "cfo"],
)


def buy(insider="A", day=1, value=30000.0, ceo=False, issuer="0000001", ticker="TST"):
    return InsiderBuy(
        accession=f"acc-{insider}-{day}",
        issuer_cik=issuer,
        ticker=ticker,
        insider_cik=insider,
        insider_name=insider,
        is_ceo_cfo=ceo,
        trans_date=date(2024, 3, day),
        filing_date=date(2024, 3, day),
        shares=value / 10.0,
        price=10.0,
    )


def test_two_insiders_within_window_fire_cluster():
    signals = detect_signals([buy("A", 1), buy("B", 5)], CFG)
    assert len(signals) == 1
    assert signals[0].profile == "form4"
    assert signals[0].trigger_date == date(2024, 3, 5)  # fires on the later filing


def test_same_insider_twice_is_not_a_cluster():
    signals = detect_signals([buy("A", 1), buy("A", 5)], CFG)
    assert signals == []


def test_window_boundary_excludes_stale_buy():
    # Mar 1 2024 (Fri) -> Mar 15 (Fri) is exactly 10 business days; Mar 18 is 11.
    signals = detect_signals([buy("A", 1), buy("B", 18)], CFG)
    assert signals == []


def test_below_value_threshold_ineligible():
    signals = detect_signals([buy("A", 1, value=24999.0), buy("B", 5)], CFG)
    assert signals == []


def test_single_ceo_buy_over_250k_fires():
    signals = detect_signals([buy("A", 4, value=250000.0, ceo=True)], CFG)
    assert len(signals) == 1


def test_single_non_officer_needs_1m():
    assert detect_signals([buy("A", 4, value=999999.0)], CFG) == []
    assert len(detect_signals([buy("A", 4, value=1000000.0)], CFG)) == 1


def test_one_signal_per_issuer_per_window():
    # Three insiders buying across 4 days: cluster fires once, not on every new buy.
    signals = detect_signals([buy("A", 1), buy("B", 4), buy("C", 5)], CFG)
    assert len(signals) == 1


def test_missing_ticker_is_skipped():
    signals = detect_signals(
        [buy("A", 1, ticker=None), buy("B", 5, ticker=None)], CFG
    )
    assert signals == []


def test_score_increases_with_cluster_size_and_ceo():
    small = detect_signals([buy("A", 1), buy("B", 5)], CFG)[0]
    big = detect_signals(
        [buy("A", 1), buy("B", 4), buy("C", 5, ceo=True)], CFG
    )[0]
    assert big.score > small.score
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scorer_form4.py -v`
Expected: FAIL with `ModuleNotFoundError` for `drifthunter.scorer.form4`

- [ ] **Step 3: Implement the scorer**

`drifthunter/scorer/form4.py`:

```python
"""Form 4 cluster-buy detection. Deterministic; shared by backtest and live.

Business-day windows use numpy weekday counting (holidays deliberately
ignored: a holiday inside the window widens it by at most a day, which is
noise relative to the 10-day window, and keeps live/backtest behavior
identical with no calendar dependency).
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np

from drifthunter.config import Form4Config
from drifthunter.scorer.models import InsiderBuy, Signal


def _bdays_between(start: date, end: date) -> int:
    return int(np.busday_count(start.isoformat(), end.isoformat()))


def detect_signals(buys: list[InsiderBuy], cfg: Form4Config) -> list[Signal]:
    eligible = [
        b for b in buys
        if b.value >= cfg.min_transaction_value and b.ticker
    ]
    by_issuer: dict[str, list[InsiderBuy]] = {}
    for b in eligible:
        by_issuer.setdefault(b.issuer_cik, []).append(b)

    signals: list[Signal] = []
    for issuer_buys in by_issuer.values():
        issuer_buys.sort(key=lambda b: (b.filing_date, b.accession))
        suppressed_until: date | None = None
        for i, b in enumerate(issuer_buys):
            if suppressed_until and _bdays_between(suppressed_until, b.filing_date) < cfg.cluster_window_bdays:
                continue
            window = [
                w for w in issuer_buys[: i + 1]
                if _bdays_between(w.filing_date, b.filing_date) <= cfg.cluster_window_bdays
            ]
            insiders = {w.insider_cik for w in window}
            total = sum(w.value for w in window)
            any_ceo = any(w.is_ceo_cfo for w in window)
            fires = (
                len(insiders) >= cfg.cluster_min_insiders
                or (b.is_ceo_cfo and b.value >= cfg.ceo_cfo_single_min)
                or b.value >= cfg.any_single_min
            )
            if not fires:
                continue
            score = (
                float(len(insiders))
                + math.log10(total / cfg.min_transaction_value)
                + (1.0 if any_ceo else 0.0)
            )
            signals.append(Signal(
                profile="form4",
                ticker=b.ticker,  # type: ignore[arg-type]  # filtered above
                trigger_date=b.filing_date,
                score=round(score, 4),
                detail=f"insiders={len(insiders)} total=${total:,.0f} ceo_cfo={any_ceo}",
            ))
            suppressed_until = b.filing_date
    signals.sort(key=lambda s: (s.trigger_date, s.ticker))
    return signals
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_scorer_form4.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add drifthunter/scorer/form4.py tests/test_scorer_form4.py
git commit -m "feat: deterministic Form 4 cluster-buy scorer"
```

---

### Task 4: 13D initiation scorer

**Files:**
- Create: `drifthunter/scorer/sc13d.py`
- Test: `tests/test_scorer_sc13d.py`

Rules from spec §3.3: new SC 13D only (amendments excluded), subject must have a ticker. Score = 2.0 base + 1.0 if filer CIK is on the activist list.

- [ ] **Step 1: Write the failing tests**

`tests/test_scorer_sc13d.py`:

```python
from datetime import date

from drifthunter.config import Sc13dConfig
from drifthunter.scorer.models import ThirteenDFiling
from drifthunter.scorer.sc13d import detect_signals


def filing(amend=False, ticker="EXM", filer="0009999999"):
    return ThirteenDFiling(
        accession="0002-24-000002",
        subject_cik="0000789019",
        subject_name="EXAMPLECO",
        ticker=ticker,
        filer_cik=filer,
        filer_name="SOME LP",
        filing_date=date(2024, 5, 6),
        is_amendment=amend,
    )


def test_new_13d_fires():
    signals = detect_signals([filing()], Sc13dConfig(activist_ciks=[]))
    assert len(signals) == 1
    assert signals[0].profile == "sc13d"
    assert signals[0].score == 2.0


def test_amendment_excluded():
    assert detect_signals([filing(amend=True)], Sc13dConfig(activist_ciks=[])) == []


def test_missing_ticker_excluded():
    assert detect_signals([filing(ticker=None)], Sc13dConfig(activist_ciks=[])) == []


def test_activist_filer_boost():
    signals = detect_signals([filing(filer="0001336528")],
                             Sc13dConfig(activist_ciks=["0001336528"]))
    assert signals[0].score == 3.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_scorer_sc13d.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`drifthunter/scorer/sc13d.py`:

```python
"""SC 13D initiation detection. Deterministic; shared by backtest and live."""
from __future__ import annotations

from drifthunter.config import Sc13dConfig
from drifthunter.scorer.models import Signal, ThirteenDFiling

BASE_SCORE = 2.0
ACTIVIST_BOOST = 1.0


def detect_signals(filings: list[ThirteenDFiling], cfg: Sc13dConfig) -> list[Signal]:
    activists = set(cfg.activist_ciks)
    signals = [
        Signal(
            profile="sc13d",
            ticker=f.ticker,  # type: ignore[arg-type]  # filtered below
            trigger_date=f.filing_date,
            score=BASE_SCORE + (ACTIVIST_BOOST if f.filer_cik in activists else 0.0),
            detail=f"filer={f.filer_name} subject={f.subject_name}",
        )
        for f in filings
        if not f.is_amendment and f.ticker
    ]
    signals.sort(key=lambda s: (s.trigger_date, s.ticker))
    return signals
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_scorer_sc13d.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add drifthunter/scorer/sc13d.py tests/test_scorer_sc13d.py
git commit -m "feat: 13D initiation scorer"
```

---

### Task 5: Rate-limited EDGAR HTTP client + ticker/exchange map

**Files:**
- Create: `drifthunter/ingest/http.py`, `drifthunter/ingest/tickers.py`
- Create: `tests/fixtures/company_tickers_exchange.json`
- Test: `tests/test_tickers.py`

- [ ] **Step 1: Create the fixture**

`tests/fixtures/company_tickers_exchange.json` (mirrors SEC's real shape — `fields` + `data` rows):

```json
{
  "fields": ["cik", "name", "ticker", "exchange"],
  "data": [
    [320193, "Apple Inc.", "AAPL", "Nasdaq"],
    [789019, "EXAMPLECO", "EXM", "NYSE"],
    [1234567, "PINKCO HOLDINGS", "PNKC", "OTC"]
  ]
}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_tickers.py`:

```python
import json
from pathlib import Path

from drifthunter.ingest.tickers import TickerMap

FIXTURE = Path(__file__).parent / "fixtures" / "company_tickers_exchange.json"


def test_lookup_by_cik_zero_padded_or_int():
    tm = TickerMap.from_json(json.loads(FIXTURE.read_text()))
    assert tm.ticker_for_cik("0000320193") == "AAPL"
    assert tm.ticker_for_cik("320193") == "AAPL"
    assert tm.exchange_for_cik("0000789019") == "NYSE"


def test_unknown_cik_returns_none():
    tm = TickerMap.from_json(json.loads(FIXTURE.read_text()))
    assert tm.ticker_for_cik("0000000001") is None


def test_listed_filter_excludes_otc():
    tm = TickerMap.from_json(json.loads(FIXTURE.read_text()))
    assert tm.is_listed("0000320193", ["NYSE", "Nasdaq"])
    assert not tm.is_listed("0001234567", ["NYSE", "Nasdaq"])
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_tickers.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement http client and ticker map**

`drifthunter/ingest/http.py`:

```python
"""Shared EDGAR-polite HTTP: rate cap, backoff, User-Agent, disk cache."""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from drifthunter.config import EdgarConfig


class EdgarClient:
    def __init__(self, cfg: EdgarConfig, cache_dir: Path):
        self._min_interval = 1.0 / cfg.max_requests_per_sec
        self._last_request = 0.0
        self._cache_dir = cache_dir
        self._client = httpx.Client(
            headers={"User-Agent": cfg.user_agent},
            timeout=30.0,
            follow_redirects=True,
        )

    def get_bytes(self, url: str, cache_key: str) -> bytes:
        """Fetch with throttle + retry; cache to disk keyed by cache_key."""
        cached = self._cache_dir / cache_key
        if cached.exists():
            return cached.read_bytes()
        for attempt in range(5):
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            resp = self._client.get(url)
            if resp.status_code in (429, 500, 502, 503):
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(resp.content)
            return resp.content
        raise RuntimeError(f"EDGAR fetch failed after retries: {url}")
```

`drifthunter/ingest/tickers.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json

from drifthunter.ingest.http import EdgarClient

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


@dataclass(frozen=True)
class TickerMap:
    _by_cik: dict[int, tuple[str, str]]  # cik -> (ticker, exchange)

    @classmethod
    def from_json(cls, raw: dict) -> TickerMap:
        idx = {f: i for i, f in enumerate(raw["fields"])}
        by_cik = {
            int(row[idx["cik"]]): (str(row[idx["ticker"]]), str(row[idx["exchange"]]))
            for row in raw["data"]
        }
        return cls(_by_cik=by_cik)

    @classmethod
    def fetch(cls, client: EdgarClient) -> TickerMap:
        return cls.from_json(json.loads(client.get_bytes(TICKERS_URL, "company_tickers_exchange.json")))

    def ticker_for_cik(self, cik: str | int) -> str | None:
        entry = self._by_cik.get(int(cik))
        return entry[0] if entry else None

    def exchange_for_cik(self, cik: str | int) -> str | None:
        entry = self._by_cik.get(int(cik))
        return entry[1] if entry else None

    def is_listed(self, cik: str | int, allowed_exchanges: list[str]) -> bool:
        return self.exchange_for_cik(cik) in allowed_exchanges
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_tickers.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add drifthunter/ingest tests/test_tickers.py tests/fixtures/company_tickers_exchange.json
git commit -m "feat: rate-limited EDGAR client and CIK-to-ticker/exchange map"
```

---

### Task 6: Insider dataset ingest (Form 4 quarterly TSVs → InsiderBuy)

**Files:**
- Create: `drifthunter/ingest/insider_datasets.py`
- Create: `tests/fixtures/form345/SUBMISSION.tsv`, `tests/fixtures/form345/NONDERIV_TRANS.tsv`, `tests/fixtures/form345/REPORTINGOWNER.tsv`
- Test: `tests/test_insider_datasets.py`

**Column-name caveat (do not skip):** the loader below is written against the documented column names of the SEC Insider Transactions Data Sets. Before trusting a full run, Step 6 downloads ONE real quarter and asserts the expected columns exist; if SEC's actual names differ, fix the `COL_*` constants — never the tests' semantics.

- [ ] **Step 1: Create TSV fixtures**

`tests/fixtures/form345/SUBMISSION.tsv`:

```
ACCESSION_NUMBER	FILING_DATE	PERIOD_OF_REPORT	DOCUMENT_TYPE	ISSUERCIK	ISSUERNAME	ISSUERTRADINGSYMBOL
0001-24-000001	04-MAR-2024	01-MAR-2024	4	789019	EXAMPLECO	EXM
0001-24-000002	05-MAR-2024	04-MAR-2024	4	789019	EXAMPLECO	EXM
0001-24-000003	05-MAR-2024	04-MAR-2024	4	320193	Apple Inc.	AAPL
0001-24-000004	06-MAR-2024	05-MAR-2024	3	320193	Apple Inc.	AAPL
```

`tests/fixtures/form345/NONDERIV_TRANS.tsv`:

```
ACCESSION_NUMBER	NONDERIV_TRANS_SK	SECURITY_TITLE	TRANS_DATE	TRANS_CODE	TRANS_ACQUIRED_DISP_CD	TRANS_SHARES	TRANS_PRICEPERSHARE
0001-24-000001	1	Common Stock	01-MAR-2024	P	A	3000	10.00
0001-24-000002	2	Common Stock	04-MAR-2024	P	A	5000	10.00
0001-24-000003	3	Common Stock	04-MAR-2024	S	D	1000	170.00
0001-24-000002	4	Common Stock	04-MAR-2024	P	A	100	10.00
```

`tests/fixtures/form345/REPORTINGOWNER.tsv`:

```
ACCESSION_NUMBER	RPTOWNERCIK	RPTOWNERNAME	RPTOWNER_RELATIONSHIP	RPTOWNER_TITLE
0001-24-000001	1111111	DOE JANE	Officer	Chief Executive Officer
0001-24-000002	2222222	ROE RICHARD	Director	
0001-24-000003	3333333	MOE MARY	Officer	VP Sales
```

- [ ] **Step 2: Write the failing tests**

`tests/test_insider_datasets.py`:

```python
from datetime import date
from pathlib import Path

from drifthunter.config import Form4Config
from drifthunter.ingest.insider_datasets import load_quarter_dir

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "form345"

CFG = Form4Config(
    min_transaction_value=25000, cluster_window_bdays=10, cluster_min_insiders=2,
    ceo_cfo_single_min=250000, any_single_min=1000000,
    ceo_cfo_title_keywords=["chief executive", "ceo", "chief financial", "cfo"],
)


def test_loads_only_code_p_acquisitions_on_form_4():
    buys = load_quarter_dir(FIXTURE_DIR, CFG)
    # 3 P/A rows exist but one belongs to a sale accession? No: rows 1, 2, 4 are P/A.
    assert len(buys) == 3
    assert all(b.issuer_cik == "789019" for b in buys)  # AAPL row was code S


def test_ceo_title_detection():
    buys = load_quarter_dir(FIXTURE_DIR, CFG)
    by_acc = {b.accession: b for b in buys if b.accession == "0001-24-000001"}
    assert by_acc["0001-24-000001"].is_ceo_cfo is True


def test_dates_and_values_parse():
    buys = load_quarter_dir(FIXTURE_DIR, CFG)
    first = next(b for b in buys if b.accession == "0001-24-000001")
    assert first.filing_date == date(2024, 3, 4)
    assert first.trans_date == date(2024, 3, 1)
    assert first.value == 30000.0
    assert first.ticker == "EXM"


def test_director_without_title_is_not_ceo_cfo():
    buys = load_quarter_dir(FIXTURE_DIR, CFG)
    director = next(b for b in buys if b.accession == "0001-24-000002")
    assert director.is_ceo_cfo is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_insider_datasets.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement the loader**

`drifthunter/ingest/insider_datasets.py`:

```python
"""Load SEC Insider Transactions Data Sets (quarterly TSV bundles) into InsiderBuy.

Bundles: https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets
Each quarter is a ZIP containing SUBMISSION.tsv, NONDERIV_TRANS.tsv,
REPORTINGOWNER.tsv (and others we ignore).
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from drifthunter.config import EdgarConfig, Form4Config
from drifthunter.ingest.http import EdgarClient
from drifthunter.scorer.models import InsiderBuy

DATASET_URL = (
    "https://www.sec.gov/files/structureddata/data/"
    "insider-transactions-data-sets/{label}_form345.zip"
)

# Column names per the SEC dataset documentation. If a real download's columns
# differ, fix these constants (verified by `verify_real_quarter`, Step 6).
COL_ACCESSION = "ACCESSION_NUMBER"
COL_FILING_DATE = "FILING_DATE"
COL_DOC_TYPE = "DOCUMENT_TYPE"
COL_ISSUER_CIK = "ISSUERCIK"
COL_ISSUER_TICKER = "ISSUERTRADINGSYMBOL"
COL_TRANS_DATE = "TRANS_DATE"
COL_TRANS_CODE = "TRANS_CODE"
COL_ACQ_DISP = "TRANS_ACQUIRED_DISP_CD"
COL_SHARES = "TRANS_SHARES"
COL_PRICE = "TRANS_PRICEPERSHARE"
COL_OWNER_CIK = "RPTOWNERCIK"
COL_OWNER_NAME = "RPTOWNERNAME"
COL_OWNER_TITLE = "RPTOWNER_TITLE"


def _read_tsv(path_or_buf) -> pd.DataFrame:
    return pd.read_csv(path_or_buf, sep="\t", dtype=str, keep_default_na=False)


def _parse_sec_date(s: str) -> date | None:
    s = s.strip()
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_ceo_cfo(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(k in t for k in keywords)


def _build_buys(sub: pd.DataFrame, trans: pd.DataFrame, owners: pd.DataFrame,
                cfg: Form4Config) -> list[InsiderBuy]:
    sub = sub[sub[COL_DOC_TYPE].str.strip() == "4"]
    trans = trans[
        (trans[COL_TRANS_CODE].str.strip() == "P")
        & (trans[COL_ACQ_DISP].str.strip() == "A")
    ]
    merged = trans.merge(sub, on=COL_ACCESSION).merge(owners, on=COL_ACCESSION)
    buys: list[InsiderBuy] = []
    for row in merged.itertuples(index=False):
        d = row._asdict() if hasattr(row, "_asdict") else dict(zip(merged.columns, row))
        filing_date = _parse_sec_date(d[COL_FILING_DATE])
        trans_date = _parse_sec_date(d[COL_TRANS_DATE])
        try:
            shares = float(d[COL_SHARES])
            price = float(d[COL_PRICE])
        except ValueError:
            continue  # missing/footnoted price or shares: unscoreable, skip
        if filing_date is None or trans_date is None or shares <= 0 or price <= 0:
            continue
        ticker = d[COL_ISSUER_TICKER].strip().upper() or None
        if ticker in {"NONE", "N/A"}:
            ticker = None
        buys.append(InsiderBuy(
            accession=d[COL_ACCESSION].strip(),
            issuer_cik=d[COL_ISSUER_CIK].strip(),
            ticker=ticker,
            insider_cik=d[COL_OWNER_CIK].strip(),
            insider_name=d[COL_OWNER_NAME].strip(),
            is_ceo_cfo=_is_ceo_cfo(d.get(COL_OWNER_TITLE, ""), cfg.ceo_cfo_title_keywords),
            trans_date=trans_date,
            filing_date=filing_date,
            shares=shares,
            price=price,
        ))
    return buys


def load_quarter_dir(dir_path: Path, cfg: Form4Config) -> list[InsiderBuy]:
    """Load from an extracted directory (used by tests and inspection)."""
    return _build_buys(
        _read_tsv(dir_path / "SUBMISSION.tsv"),
        _read_tsv(dir_path / "NONDERIV_TRANS.tsv"),
        _read_tsv(dir_path / "REPORTINGOWNER.tsv"),
        cfg,
    )


def load_quarter_zip(zip_bytes: bytes, cfg: Form4Config) -> list[InsiderBuy]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        def read(name: str) -> pd.DataFrame:
            return _read_tsv(io.BytesIO(zf.read(name)))
        return _build_buys(
            read("SUBMISSION.tsv"), read("NONDERIV_TRANS.tsv"),
            read("REPORTINGOWNER.tsv"), cfg,
        )


def quarter_labels(start: date, end: date) -> list[str]:
    """['2021q2', '2021q3', ...] covering [start, end]."""
    labels = []
    y, q = start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        labels.append(f"{y}q{q}")
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return labels


def download_quarters(client: EdgarClient, start: date, end: date,
                      cfg: Form4Config) -> list[InsiderBuy]:
    buys: list[InsiderBuy] = []
    for label in quarter_labels(start, end):
        raw = client.get_bytes(DATASET_URL.format(label=label), f"form345/{label}.zip")
        buys.extend(load_quarter_zip(raw, cfg))
    return buys
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_insider_datasets.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Verify against one real quarter (manual, requires network)**

Run:

```bash
uv run python -c "
from pathlib import Path
from datetime import date
from drifthunter.config import load_config
from drifthunter.ingest.http import EdgarClient
from drifthunter.ingest import insider_datasets as ids
cfg = load_config(Path('config.yaml'))
client = EdgarClient(cfg.edgar, cfg.data_dir / 'cache')
raw = client.get_bytes(ids.DATASET_URL.format(label='2024q1'), 'form345/2024q1.zip')
buys = ids.load_quarter_zip(raw, cfg.form4)
print(f'{len(buys)} buys loaded; sample: {buys[0]}')"
```

Expected: several thousand buys print without exception. If it raises `KeyError` on a column name, compare against the ZIP's actual TSV headers and fix the `COL_*` constants only (and, if the URL pattern 404s, fix `DATASET_URL` from the SEC datasets page), then re-run Steps 5 and 6.

- [ ] **Step 7: Commit**

```bash
git add drifthunter/ingest/insider_datasets.py tests/test_insider_datasets.py tests/fixtures/form345
git commit -m "feat: Form 4 quarterly dataset ingest to InsiderBuy records"
```

---

### Task 7: SC 13D ingest (form index + header parse)

**Files:**
- Create: `drifthunter/ingest/form_index.py`, `drifthunter/ingest/header_parse.py`
- Create: `tests/fixtures/form.idx`, `tests/fixtures/sc13d_header.txt`
- Test: `tests/test_form_index.py`, `tests/test_header_parse.py`

- [ ] **Step 1: Create fixtures**

`tests/fixtures/form.idx` (fixed-width, mirrors real EDGAR full-index layout: header block, dashed line, then rows — Form Type starts at column 0, Company Name at 12, CIK at 74, Date Filed at 86, File Name at 98; the parser below splits on 2+ spaces instead of fixed columns for robustness):

```
Description:           Master Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    March 31, 2024

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------
SC 13D      ACTIVIST CAPITAL LP                                           1336528     2024-03-04  edgar/data/789019/0001336528-24-000012.txt
SC 13D/A    OTHER FUND LLC                                                1400000     2024-03-05  edgar/data/320193/0001400000-24-000033.txt
SC 13G      PASSIVE HOLDINGS INC                                          1500000     2024-03-05  edgar/data/320193/0001500000-24-000044.txt
SC 13D      SECOND ACTIVIST LP                                            1600000     2024-03-06  edgar/data/999999/0001600000-24-000055.txt
```

`tests/fixtures/sc13d_header.txt` (first bytes of a real-format .txt submission, through `</SEC-HEADER>`):

```
<SEC-DOCUMENT>0001336528-24-000012.txt : 20240304
<SEC-HEADER>0001336528-24-000012.hdr.sgml : 20240304
ACCESSION NUMBER:		0001336528-24-000012
CONFORMED SUBMISSION TYPE:	SC 13D
PUBLIC DOCUMENT COUNT:		2
FILED AS OF DATE:		20240304

SUBJECT COMPANY:	

	COMPANY DATA:	
		COMPANY CONFORMED NAME:			EXAMPLECO
		CENTRAL INDEX KEY:			0000789019
		STANDARD INDUSTRIAL CLASSIFICATION:	SERVICES [7372]

FILED BY:		

	COMPANY DATA:	
		COMPANY CONFORMED NAME:			ACTIVIST CAPITAL LP
		CENTRAL INDEX KEY:			0001336528
</SEC-HEADER>
<DOCUMENT>
```

- [ ] **Step 2: Write the failing tests**

`tests/test_form_index.py`:

```python
from datetime import date
from pathlib import Path

from drifthunter.ingest.form_index import parse_form_idx

FIXTURE = Path(__file__).parent / "fixtures" / "form.idx"


def test_extracts_only_new_sc13d_rows():
    rows = parse_form_idx(FIXTURE.read_text())
    assert len(rows) == 2  # SC 13D/A and SC 13G excluded
    assert rows[0].filer_name == "ACTIVIST CAPITAL LP"
    assert rows[0].date_filed == date(2024, 3, 4)
    assert rows[0].path == "edgar/data/789019/0001336528-24-000012.txt"
```

`tests/test_header_parse.py`:

```python
from pathlib import Path

from drifthunter.ingest.header_parse import parse_header

FIXTURE = Path(__file__).parent / "fixtures" / "sc13d_header.txt"


def test_parses_subject_and_filer():
    hdr = parse_header(FIXTURE.read_text())
    assert hdr.subject_cik == "0000789019"
    assert hdr.subject_name == "EXAMPLECO"
    assert hdr.filer_cik == "0001336528"
    assert hdr.filer_name == "ACTIVIST CAPITAL LP"
    assert hdr.accession == "0001336528-24-000012"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_form_index.py tests/test_header_parse.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement both parsers**

`drifthunter/ingest/form_index.py`:

```python
"""Parse EDGAR quarterly form.idx for new SC 13D filings.

Index URL pattern:
https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from drifthunter.ingest.http import EdgarClient

INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"


@dataclass(frozen=True)
class IndexRow:
    form_type: str
    filer_name: str
    filer_cik: str
    date_filed: date
    path: str


def parse_form_idx(text: str) -> list[IndexRow]:
    rows: list[IndexRow] = []
    for line in text.splitlines():
        if not line.startswith("SC 13D "):  # trailing space: excludes 'SC 13D/A'
            continue
        # columns separated by runs of 2+ spaces
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form_type, name, cik, filed, path = parts[0], parts[1], parts[2], parts[3], parts[4]
        if form_type != "SC 13D":
            continue
        rows.append(IndexRow(
            form_type=form_type,
            filer_name=name,
            filer_cik=cik,
            date_filed=datetime.strptime(filed, "%Y-%m-%d").date(),
            path=path,
        ))
    return rows


def fetch_quarter(client: EdgarClient, year: int, q: int) -> list[IndexRow]:
    raw = client.get_bytes(INDEX_URL.format(year=year, q=q), f"form_idx/{year}q{q}.idx")
    return parse_form_idx(raw.decode("latin-1"))
```

`drifthunter/ingest/header_parse.py`:

```python
"""Parse the SGML header at the top of an EDGAR .txt submission.

We only need SUBJECT COMPANY and FILED BY blocks; fetch is bounded by
reading until </SEC-HEADER> (headers are within the first few KB).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from drifthunter.ingest.http import EdgarClient

ARCHIVES_BASE = "https://www.sec.gov/Archives/"


@dataclass(frozen=True)
class FilingHeader:
    accession: str
    subject_cik: str
    subject_name: str
    filer_cik: str
    filer_name: str


def _block_field(block: str, field: str) -> str:
    m = re.search(rf"{field}:\s*(.+)", block)
    return m.group(1).strip() if m else ""


def parse_header(text: str) -> FilingHeader:
    header = text.split("</SEC-HEADER>")[0]
    accession = _block_field(header, "ACCESSION NUMBER")

    def section(name: str) -> str:
        m = re.search(rf"{name}:.*?(?=\n[A-Z][A-Z ]+:|\Z)", header, re.DOTALL)
        return m.group(0) if m else ""

    subject = section("SUBJECT COMPANY")
    filed_by = section("FILED BY")
    return FilingHeader(
        accession=accession,
        subject_cik=_block_field(subject, "CENTRAL INDEX KEY"),
        subject_name=_block_field(subject, "COMPANY CONFORMED NAME"),
        filer_cik=_block_field(filed_by, "CENTRAL INDEX KEY"),
        filer_name=_block_field(filed_by, "COMPANY CONFORMED NAME"),
    )


def fetch_header(client: EdgarClient, path: str) -> FilingHeader:
    accession_key = path.replace("/", "_")
    raw = client.get_bytes(ARCHIVES_BASE + path, f"headers/{accession_key}")
    return parse_header(raw.decode("latin-1", errors="replace"))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_form_index.py tests/test_header_parse.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add drifthunter/ingest/form_index.py drifthunter/ingest/header_parse.py \
        tests/test_form_index.py tests/test_header_parse.py \
        tests/fixtures/form.idx tests/fixtures/sc13d_header.txt
git commit -m "feat: SC 13D form-index and filing-header parsers"
```

---

### Task 8: Price provider protocol, parquet cache, free provider, coverage report

**Files:**
- Create: `drifthunter/prices/provider.py`, `drifthunter/prices/free.py`
- Test: `tests/test_prices.py`

All providers return a DataFrame indexed by `date` (datetime64, ascending, unique) with float columns `open`, `close`, `volume`, or an **empty** DataFrame when the ticker is unavailable. The cache wrapper and coverage report work for any provider.

- [ ] **Step 1: Write the failing tests**

`tests/test_prices.py`:

```python
from datetime import date

import pandas as pd
import pytest

from drifthunter.prices.provider import CachingProvider, coverage_report


class FakeProvider:
    def __init__(self, available: dict[str, pd.DataFrame]):
        self.available = available
        self.calls = 0

    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        self.calls += 1
        return self.available.get(ticker, pd.DataFrame())


def make_prices(n=30, start="2024-01-02"):
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame(
        {"open": 10.0, "close": 10.0, "volume": 200_000.0}, index=idx
    ).rename_axis("date")


def test_cache_avoids_second_fetch(tmp_path):
    inner = FakeProvider({"EXM": make_prices()})
    p = CachingProvider(inner, tmp_path)
    a = p.daily("EXM", date(2024, 1, 2), date(2024, 2, 9))
    b = p.daily("EXM", date(2024, 1, 2), date(2024, 2, 9))
    assert inner.calls == 1
    pd.testing.assert_frame_equal(a, b)


def test_missing_ticker_returns_empty(tmp_path):
    p = CachingProvider(FakeProvider({}), tmp_path)
    assert p.daily("GONE", date(2024, 1, 2), date(2024, 2, 9)).empty


def test_coverage_report(tmp_path):
    p = CachingProvider(FakeProvider({"EXM": make_prices()}), tmp_path)
    cov = coverage_report(["EXM", "GONE", "EXM"], p, date(2024, 1, 2), date(2024, 2, 9))
    assert cov.total == 2          # deduped
    assert cov.covered == 1
    assert cov.rate == pytest.approx(0.5)
    assert cov.missing == ["GONE"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_prices.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement provider module**

`drifthunter/prices/provider.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import pandas as pd

COLUMNS = ["open", "close", "volume"]


class PriceProvider(Protocol):
    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame: ...


class CachingProvider:
    """Wraps any provider with a per-ticker parquet cache (empty results cached too,
    as zero-row frames, so dead tickers aren't re-fetched)."""

    def __init__(self, inner: PriceProvider, cache_dir: Path):
        self._inner = inner
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        key = self._dir / f"{ticker}_{start.isoformat()}_{end.isoformat()}.parquet"
        if key.exists():
            return pd.read_parquet(key)
        df = self._inner.daily(ticker, start, end)
        if not df.empty:
            df = df[COLUMNS].astype(float).sort_index()
            df = df[~df.index.duplicated(keep="first")]
        else:
            df = pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="date"))
        df.to_parquet(key)
        return df


@dataclass(frozen=True)
class CoverageReport:
    total: int
    covered: int
    missing: list[str]

    @property
    def rate(self) -> float:
        return self.covered / self.total if self.total else 0.0


def coverage_report(tickers: list[str], provider: PriceProvider,
                    start: date, end: date) -> CoverageReport:
    unique = sorted(set(tickers))
    missing = [t for t in unique if provider.daily(t, start, end).empty]
    return CoverageReport(total=len(unique), covered=len(unique) - len(missing),
                          missing=missing)
```

`drifthunter/prices/free.py`:

```python
"""Free fallback chain: yfinance first, Stooq CSV second.

Spec §3.2: this path is the fallback; expect imperfect delisted-ticker
coverage and rely on the coverage report to quantify it.
"""
from __future__ import annotations

import io
from datetime import date

import httpx
import pandas as pd

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}.us&d1={d1}&d2={d2}&i=d"


class FreeProvider:
    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        df = self._yfinance(ticker, start, end)
        if df.empty:
            df = self._stooq(ticker, start, end)
        return df

    @staticmethod
    def _yfinance(ticker: str, start: date, end: date) -> pd.DataFrame:
        import yfinance as yf
        raw = yf.download(ticker, start=start.isoformat(), end=end.isoformat(),
                          progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            return pd.DataFrame()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        out = raw.rename(columns={"Open": "open", "Close": "close", "Volume": "volume"})
        return out[["open", "close", "volume"]].rename_axis("date")

    @staticmethod
    def _stooq(ticker: str, start: date, end: date) -> pd.DataFrame:
        url = STOOQ_URL.format(symbol=ticker.lower(),
                               d1=start.strftime("%Y%m%d"), d2=end.strftime("%Y%m%d"))
        try:
            resp = httpx.get(url, timeout=30.0)
            resp.raise_for_status()
        except httpx.HTTPError:
            return pd.DataFrame()
        if not resp.text.startswith("Date,"):
            return pd.DataFrame()
        raw = pd.read_csv(io.StringIO(resp.text), parse_dates=["Date"], index_col="Date")
        out = raw.rename(columns={"Open": "open", "Close": "close", "Volume": "volume"})
        return out[["open", "close", "volume"]].rename_axis("date")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_prices.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add drifthunter/prices tests/test_prices.py
git commit -m "feat: price provider protocol, parquet cache, free fallback, coverage report"
```

---

### Task 9: Sharadar provider (paid primary)

**Files:**
- Create: `drifthunter/prices/sharadar.py`
- Test: `tests/test_sharadar.py`

- [ ] **Step 1: Write the failing test (mocked SDK — never hits the network in tests)**

`tests/test_sharadar.py`:

```python
from datetime import date

import pandas as pd

from drifthunter.prices.sharadar import SharadarProvider


def test_normalizes_sep_table(monkeypatch):
    fake = pd.DataFrame({
        "ticker": ["EXM", "EXM"],
        "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "open": [10.0, 10.5],
        "close": [10.4, 10.2],
        "volume": [1_000_000.0, 900_000.0],
    })
    provider = SharadarProvider(api_key="test")
    monkeypatch.setattr(provider, "_get_table", lambda ticker, start, end: fake)
    df = provider.daily("EXM", date(2024, 1, 1), date(2024, 1, 31))
    assert list(df.columns) == ["open", "close", "volume"]
    assert df.index.name == "date"
    assert len(df) == 2


def test_empty_result_for_unknown_ticker(monkeypatch):
    provider = SharadarProvider(api_key="test")
    monkeypatch.setattr(provider, "_get_table",
                        lambda ticker, start, end: pd.DataFrame())
    assert provider.daily("GONE", date(2024, 1, 1), date(2024, 1, 31)).empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sharadar.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`drifthunter/prices/sharadar.py`:

```python
"""Sharadar Equity Prices (SEP) via Nasdaq Data Link — survivorship-bias-free.

Spec §3.1: primary Phase 0 price source. Subscribe for one month, run the
study (results land in the parquet cache), cancel.
"""
from __future__ import annotations

from datetime import date

import pandas as pd


class SharadarProvider:
    def __init__(self, api_key: str):
        self._api_key = api_key

    def _get_table(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        import nasdaqdatalink
        nasdaqdatalink.ApiConfig.api_key = self._api_key
        return nasdaqdatalink.get_table(
            "SHARADAR/SEP",
            ticker=ticker,
            date={"gte": start.isoformat(), "lte": end.isoformat()},
            paginate=True,
        )

    def daily(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        raw = self._get_table(ticker, start, end)
        if raw is None or raw.empty:
            return pd.DataFrame()
        out = raw[["date", "open", "close", "volume"]].copy()
        out["date"] = pd.to_datetime(out["date"])
        return out.set_index("date").sort_index()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sharadar.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add drifthunter/prices/sharadar.py tests/test_sharadar.py
git commit -m "feat: Sharadar SEP provider (survivorship-free primary)"
```

---

### Task 10: Event study core

**Files:**
- Create: `drifthunter/backtest/event_study.py`
- Test: `tests/test_event_study.py`

Per spec §3.4: entry at the **open of the first trading day strictly after** `trigger_date` (trading days defined by the ticker's own price index); exit at the open `horizon` rows later; per-event net return = raw return − round-trip cost; excess = net − SPY return over the identical dates. Tradability filters (spec §3.3) applied at entry: entry open ≥ `min_price`, trailing-`adv_window` average dollar volume (close×volume of the rows before entry) ≥ `min_avg_dollar_volume`. Events that fail get a `filter_reason` and are excluded from stats but reported.

- [ ] **Step 1: Write the failing tests**

`tests/test_event_study.py`:

```python
from datetime import date

import pandas as pd
import pytest

from drifthunter.config import TradabilityConfig
from drifthunter.backtest.event_study import run_event_study
from drifthunter.scorer.models import Signal

TRAD = TradabilityConfig(min_price=2.0, min_avg_dollar_volume=1_000_000,
                         adv_window=3, allowed_exchanges=["NYSE", "Nasdaq"])


def prices(opens, start="2024-03-01", volume=500_000.0):
    idx = pd.bdate_range(start, periods=len(opens))
    return pd.DataFrame(
        {"open": opens, "close": opens, "volume": volume}, index=idx
    ).rename_axis("date")


class DictProvider:
    def __init__(self, frames):
        self.frames = frames

    def daily(self, ticker, start, end):
        return self.frames.get(ticker, pd.DataFrame())


def test_entry_exit_and_excess_return():
    # Ticker rises 10.0 -> 11.0 between entry (Mar 4 open) and exit (Mar 6 open, horizon 2).
    # SPY flat. Cost 30bp. Expected excess = 0.10 - 0.003 - 0.0 = 0.097
    frames = {
        "EXM": prices([9.0, 10.0, 10.5, 11.0, 11.2], volume=500_000.0),
        "SPY": prices([400.0] * 5, volume=10_000_000.0),
    }
    sig = Signal(profile="form4", ticker="EXM", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[2], cost_bps_list=[30], benchmark="SPY")
    row = result[(result.horizon == 2) & (result.cost_bps == 30)].iloc[0]
    assert row["entry_date"] == pd.Timestamp("2024-03-04")
    assert row["excess_return"] == pytest.approx(0.097, abs=1e-9)
    assert row["filter_reason"] == ""


def test_low_price_filtered():
    frames = {
        "PNY": prices([1.5, 1.6, 1.7, 1.8, 1.9]),
        "SPY": prices([400.0] * 5),
    }
    sig = Signal(profile="form4", ticker="PNY", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[2], cost_bps_list=[30], benchmark="SPY")
    assert result.iloc[0]["filter_reason"] == "min_price"


def test_low_dollar_volume_filtered():
    frames = {
        "THIN": prices([10.0] * 5, volume=10_000.0),  # $100k/day << $1M
        "SPY": prices([400.0] * 5),
    }
    sig = Signal(profile="form4", ticker="THIN", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[2], cost_bps_list=[30], benchmark="SPY")
    assert result.iloc[0]["filter_reason"] == "adv"


def test_missing_prices_marked_uncovered():
    frames = {"SPY": prices([400.0] * 5)}
    sig = Signal(profile="sc13d", ticker="GONE", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[2], cost_bps_list=[30], benchmark="SPY")
    assert result.iloc[0]["filter_reason"] == "no_prices"


def test_insufficient_future_rows_skipped():
    frames = {
        "EXM": prices([10.0, 10.0, 10.0]),  # horizon 40 cannot complete
        "SPY": prices([400.0] * 3),
    }
    sig = Signal(profile="form4", ticker="EXM", trigger_date=date(2024, 3, 1),
                 score=2.0, detail="")
    result = run_event_study([sig], DictProvider(frames), TRAD,
                             horizons=[40], cost_bps_list=[30], benchmark="SPY")
    assert result.iloc[0]["filter_reason"] == "insufficient_history"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_event_study.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`drifthunter/backtest/event_study.py`:

```python
"""Per-event excess returns over a horizon x cost sweep (spec §3.4)."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from drifthunter.config import TradabilityConfig
from drifthunter.prices.provider import PriceProvider
from drifthunter.scorer.models import Signal

# Fetch margin so entry+horizon rows and the ADV lookback always fit.
LOOKBACK_CAL_DAYS = 60
LOOKAHEAD_CAL_DAYS = 90

RESULT_COLUMNS = [
    "profile", "ticker", "trigger_date", "score", "horizon", "cost_bps",
    "entry_date", "exit_date", "raw_return", "excess_return", "filter_reason",
]


def _event_rows(sig: Signal, provider: PriceProvider, trad: TradabilityConfig,
                horizons: list[int], cost_bps_list: list[int],
                benchmark: str) -> list[dict]:
    start = sig.trigger_date - timedelta(days=LOOKBACK_CAL_DAYS)
    end = sig.trigger_date + timedelta(days=LOOKAHEAD_CAL_DAYS + 2 * max(horizons))
    px = provider.daily(sig.ticker, start, end)
    spy = provider.daily(benchmark, start, end)

    def rows_with_reason(reason: str) -> list[dict]:
        return [{
            "profile": sig.profile, "ticker": sig.ticker,
            "trigger_date": pd.Timestamp(sig.trigger_date), "score": sig.score,
            "horizon": h, "cost_bps": c, "entry_date": pd.NaT, "exit_date": pd.NaT,
            "raw_return": float("nan"), "excess_return": float("nan"),
            "filter_reason": reason,
        } for h in horizons for c in cost_bps_list]

    if px.empty or spy.empty:
        return rows_with_reason("no_prices")

    future = px[px.index > pd.Timestamp(sig.trigger_date)]
    if future.empty:
        return rows_with_reason("insufficient_history")
    entry_date = future.index[0]
    entry_pos = px.index.get_loc(entry_date)
    entry_open = float(px.iloc[entry_pos]["open"])

    if entry_open < trad.min_price:
        return rows_with_reason("min_price")
    pre = px.iloc[max(0, entry_pos - trad.adv_window):entry_pos]
    if len(pre) < trad.adv_window:
        return rows_with_reason("insufficient_history")
    adv = float((pre["close"] * pre["volume"]).mean())
    if adv < trad.min_avg_dollar_volume:
        return rows_with_reason("adv")

    out: list[dict] = []
    for h in horizons:
        exit_pos = entry_pos + h
        if exit_pos >= len(px):
            out.extend([r for r in rows_with_reason("insufficient_history")
                        if r["horizon"] == h])
            continue
        exit_date = px.index[exit_pos]
        exit_open = float(px.iloc[exit_pos]["open"])
        raw = exit_open / entry_open - 1.0
        if entry_date not in spy.index or exit_date not in spy.index:
            out.extend([r for r in rows_with_reason("benchmark_gap")
                        if r["horizon"] == h])
            continue
        spy_ret = float(spy.loc[exit_date, "open"]) / float(spy.loc[entry_date, "open"]) - 1.0
        for c in cost_bps_list:
            net = raw - c / 10_000.0
            out.append({
                "profile": sig.profile, "ticker": sig.ticker,
                "trigger_date": pd.Timestamp(sig.trigger_date), "score": sig.score,
                "horizon": h, "cost_bps": c,
                "entry_date": entry_date, "exit_date": exit_date,
                "raw_return": raw, "excess_return": net - spy_ret,
                "filter_reason": "",
            })
    return out


def run_event_study(signals: list[Signal], provider: PriceProvider,
                    trad: TradabilityConfig, horizons: list[int],
                    cost_bps_list: list[int], benchmark: str) -> pd.DataFrame:
    rows: list[dict] = []
    for sig in signals:
        rows.extend(_event_rows(sig, provider, trad, horizons, cost_bps_list, benchmark))
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_event_study.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add drifthunter/backtest/event_study.py tests/test_event_study.py
git commit -m "feat: event study with tradability filters and horizon/cost sweep"
```

---

### Task 11: Bootstrap CI + yearly breakdown

**Files:**
- Create: `drifthunter/backtest/stats.py`
- Test: `tests/test_stats.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_stats.py`:

```python
import numpy as np
import pandas as pd
import pytest

from drifthunter.backtest.stats import bootstrap_ci, yearly_means


def test_bootstrap_ci_deterministic_and_sane():
    rng = np.random.default_rng(0)
    values = rng.normal(0.01, 0.05, size=500)
    a = bootstrap_ci(values, n_iter=2000, seed=42)
    b = bootstrap_ci(values, n_iter=2000, seed=42)
    assert a == b                       # determinism
    assert a.lo < a.mean < a.hi
    assert a.mean == pytest.approx(values.mean(), abs=1e-12)


def test_bootstrap_ci_excludes_zero_for_strong_effect():
    values = np.full(300, 0.02) + np.random.default_rng(1).normal(0, 0.001, 300)
    ci = bootstrap_ci(values, n_iter=2000, seed=42)
    assert ci.lo > 0


def test_yearly_means():
    df = pd.DataFrame({
        "trigger_date": pd.to_datetime(["2021-05-01", "2021-06-01", "2022-05-01"]),
        "excess_return": [0.10, -0.02, 0.05],
    })
    by_year = yearly_means(df)
    assert by_year[2021] == pytest.approx(0.04)
    assert by_year[2022] == pytest.approx(0.05)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_stats.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`drifthunter/backtest/stats.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BootstrapCI:
    mean: float
    lo: float
    hi: float
    n: int


def bootstrap_ci(values, n_iter: int, seed: int, alpha: float = 0.05) -> BootstrapCI:
    arr = np.asarray(values, dtype=float)
    arr = arr[~np.isnan(arr)]
    rng = np.random.default_rng(seed)
    samples = rng.choice(arr, size=(n_iter, arr.size), replace=True)
    means = samples.mean(axis=1)
    return BootstrapCI(
        mean=float(arr.mean()),
        lo=float(np.quantile(means, alpha / 2)),
        hi=float(np.quantile(means, 1 - alpha / 2)),
        n=int(arr.size),
    )


def yearly_means(df: pd.DataFrame) -> dict[int, float]:
    """Mean excess_return grouped by trigger_date year."""
    grouped = df.groupby(df["trigger_date"].dt.year)["excess_return"].mean()
    return {int(y): float(v) for y, v in grouped.items()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_stats.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add drifthunter/backtest/stats.py tests/test_stats.py
git commit -m "feat: seeded bootstrap CI and yearly breakdown"
```

---

### Task 12: Portfolio simulator

**Files:**
- Create: `drifthunter/backtest/portfolio.py`
- Test: `tests/test_portfolio.py`

Per spec §3.5: chronological replay of the **completed event rows** at one (horizon, cost) pair. Rules: $5k bankroll, ~$1k per position, max 5 concurrent, max 2 entries/day, skip when full or out of cash (count skips), no bumping. Equity = cash + sum of open position values (marked at entry; realized at exit — daily mark-to-market is deliberately out of scope, so max drawdown is computed on the realized equity curve).

- [ ] **Step 1: Write the failing tests**

`tests/test_portfolio.py`:

```python
import pandas as pd
import pytest

from drifthunter.config import PortfolioConfig
from drifthunter.backtest.portfolio import simulate

CFG = PortfolioConfig(bankroll=5000.0, position_size=1000.0,
                      max_positions=2, max_entries_per_day=1)


def event(ticker, entry, exit_, ret, score=2.0):
    return {
        "profile": "form4", "ticker": ticker, "score": score,
        "trigger_date": pd.Timestamp(entry) - pd.Timedelta(days=1),
        "entry_date": pd.Timestamp(entry), "exit_date": pd.Timestamp(exit_),
        "raw_return": ret, "excess_return": ret, "filter_reason": "",
        "horizon": 2, "cost_bps": 30,
    }


def test_pnl_of_single_trade():
    df = pd.DataFrame([event("A", "2024-03-04", "2024-03-06", 0.10)])
    result = simulate(df, CFG)
    assert result.final_equity == pytest.approx(5000.0 + 1000.0 * 0.10)
    assert result.trades_taken == 1
    assert result.skipped_full_book == 0


def test_max_positions_enforced():
    df = pd.DataFrame([
        event("A", "2024-03-04", "2024-03-20", 0.0),
        event("B", "2024-03-05", "2024-03-20", 0.0),
        event("C", "2024-03-06", "2024-03-20", 0.0),  # book full -> skipped
    ])
    result = simulate(df, CFG)
    assert result.trades_taken == 2
    assert result.skipped_full_book == 1


def test_max_entries_per_day_enforced():
    df = pd.DataFrame([
        event("A", "2024-03-04", "2024-03-20", 0.0, score=3.0),
        event("B", "2024-03-04", "2024-03-20", 0.0, score=1.0),  # same day, cap 1
    ])
    result = simulate(df, CFG)
    assert result.trades_taken == 1
    assert result.skipped_entry_cap == 1


def test_higher_score_wins_same_day():
    df = pd.DataFrame([
        event("LOW", "2024-03-04", "2024-03-20", 0.0, score=1.0),
        event("HIGH", "2024-03-04", "2024-03-20", 0.0, score=9.0),
    ])
    result = simulate(df, CFG)
    assert result.tickers_traded == ["HIGH"]


def test_max_drawdown_on_realized_curve():
    df = pd.DataFrame([
        event("A", "2024-03-04", "2024-03-06", -0.50),
        event("B", "2024-03-07", "2024-03-11", 0.10),
    ])
    result = simulate(df, CFG)
    # After A: equity 4500 (dd = 500/5000 = 10%). B adds +100.
    assert result.max_drawdown == pytest.approx(0.10)
    assert result.final_equity == pytest.approx(4600.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_portfolio.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`drifthunter/backtest/portfolio.py`:

```python
"""Capital-constrained chronological replay (spec §3.5)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from drifthunter.config import PortfolioConfig


@dataclass
class SimResult:
    final_equity: float
    max_drawdown: float
    trades_taken: int
    skipped_full_book: int
    skipped_entry_cap: int
    skipped_no_cash: int
    tickers_traded: list[str] = field(default_factory=list)
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)


def simulate(events: pd.DataFrame, cfg: PortfolioConfig) -> SimResult:
    """events: completed rows for ONE (horizon, cost_bps) pair.
    Columns required: ticker, score, entry_date, exit_date, raw_return."""
    df = events[events["filter_reason"] == ""].copy()
    df = df.sort_values(["entry_date", "score"], ascending=[True, False])

    cash = cfg.bankroll
    open_positions: list[dict] = []   # {ticker, exit_date, size, ret}
    equity = cfg.bankroll
    peak = equity
    max_dd = 0.0
    curve: list[tuple[pd.Timestamp, float]] = []
    taken: list[str] = []
    skipped_full = skipped_cap = skipped_cash = 0
    entries_today: tuple[pd.Timestamp | None, int] = (None, 0)

    def settle_exits(now: pd.Timestamp):
        nonlocal cash, equity, peak, max_dd
        due = [p for p in open_positions if p["exit_date"] <= now]
        for p in due:
            open_positions.remove(p)
            proceeds = p["size"] * (1.0 + p["ret"])
            cash += proceeds
            equity = cash + sum(q["size"] for q in open_positions)
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
            curve.append((p["exit_date"], equity))

    for row in df.itertuples(index=False):
        settle_exits(row.entry_date)
        day_count = entries_today[1] if entries_today[0] == row.entry_date else 0
        if day_count >= cfg.max_entries_per_day:
            skipped_cap += 1
            continue
        if len(open_positions) >= cfg.max_positions:
            skipped_full += 1
            continue
        size = min(cfg.position_size, cash)
        if size < cfg.position_size * 0.5:
            skipped_cash += 1
            continue
        cash -= size
        open_positions.append({
            "ticker": row.ticker, "exit_date": row.exit_date,
            "size": size, "ret": row.raw_return,
        })
        taken.append(row.ticker)
        entries_today = (row.entry_date, day_count + 1)

    settle_exits(pd.Timestamp.max)
    return SimResult(
        final_equity=round(cash, 6),
        max_drawdown=round(max_dd, 6),
        trades_taken=len(taken),
        skipped_full_book=skipped_full,
        skipped_entry_cap=skipped_cap,
        skipped_no_cash=skipped_cash,
        tickers_traded=taken,
        equity_curve=curve,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_portfolio.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add drifthunter/backtest/portfolio.py tests/test_portfolio.py
git commit -m "feat: capital-constrained portfolio simulator"
```

---

### Task 13: Gate verdict + markdown report

**Files:**
- Create: `drifthunter/backtest/report.py`
- Test: `tests/test_report.py`

Implements spec §3.6 verbatim. Per profile, at the **headline cost** and each horizon: GO requires (a) enough covered events, (b) bootstrap CI lo > 0, (c) ≥ `min_years_positive` positive years, (d) portfolio sim positive net annualized excess vs SPY with max drawdown < limit. A profile passes if ANY horizon passes all four. Coverage below `min_coverage` taints the profile (verdict reported as `SUSPECT-<verdict>`).

- [ ] **Step 1: Write the failing tests**

`tests/test_report.py`:

```python
import numpy as np
import pandas as pd

from drifthunter.config import GateConfig, PortfolioConfig
from drifthunter.backtest.report import evaluate_gate, render_markdown

GATE = GateConfig(form4_min_events=10, sc13d_min_events=5, min_years_positive=3,
                  total_years=5, max_drawdown=0.25, min_coverage=0.80)
PORT = PortfolioConfig(bankroll=5000.0, position_size=1000.0,
                       max_positions=5, max_entries_per_day=2)


def make_events(profile, n, mean_ret, horizon=10, cost=30, start_year=2021):
    rng = np.random.default_rng(7)
    dates = pd.to_datetime([
        f"{start_year + (i % 5)}-{(i % 12) + 1:02d}-15" for i in range(n)
    ])
    entry = dates + pd.Timedelta(days=1)
    return pd.DataFrame({
        "profile": profile, "ticker": [f"T{i}" for i in range(n)],
        "score": 2.0, "trigger_date": dates, "horizon": horizon, "cost_bps": cost,
        "entry_date": entry, "exit_date": entry + pd.Timedelta(days=horizon),
        "raw_return": rng.normal(mean_ret, 0.01, n),
        "excess_return": rng.normal(mean_ret, 0.01, n),
        "filter_reason": "",
    })


def test_strong_profile_goes():
    events = make_events("form4", 200, mean_ret=0.03)
    verdict = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                            gate=GATE, port=PORT, headline_cost_bps=30,
                            horizons=[10], seed=42, spy_annual_return=0.0)
    assert verdict.decision == "GO"


def test_zero_edge_profile_kills():
    events = make_events("form4", 200, mean_ret=0.0)
    verdict = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                            gate=GATE, port=PORT, headline_cost_bps=30,
                            horizons=[10], seed=42, spy_annual_return=0.0)
    assert verdict.decision == "KILL"


def test_too_few_events_kills():
    events = make_events("form4", 5, mean_ret=0.05)
    verdict = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                            gate=GATE, port=PORT, headline_cost_bps=30,
                            horizons=[10], seed=42, spy_annual_return=0.0)
    assert verdict.decision == "KILL"
    assert "events" in verdict.reasons[0]


def test_low_coverage_taints_verdict():
    events = make_events("form4", 200, mean_ret=0.03)
    verdict = evaluate_gate(events, coverage_rate=0.5, profile="form4",
                            gate=GATE, port=PORT, headline_cost_bps=30,
                            horizons=[10], seed=42, spy_annual_return=0.0)
    assert verdict.decision == "SUSPECT-GO"


def test_markdown_renders_both_profiles():
    events = make_events("form4", 200, mean_ret=0.03)
    v = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                      gate=GATE, port=PORT, headline_cost_bps=30,
                      horizons=[10], seed=42, spy_annual_return=0.0)
    md = render_markdown([v], events)
    assert "form4" in md and "GO" in md and "Coverage" in md
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`drifthunter/backtest/report.py`:

```python
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
                  spy_annual_return: float) -> GateVerdict:
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
        ci = bootstrap_ci(hdf["excess_return"].to_numpy(), n_iter=10_000, seed=seed)
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
        reasons.append(f"0 completed events at {headline_cost_bps}bp (need {min_events})")
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
                  f"Reasons: {'; '.join(v.reasons)}", "",
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
    filtered = events[events["filter_reason"] != ""]
    if not filtered.empty:
        lines += ["## Excluded events by reason", ""]
        counts = filtered.groupby("filter_reason")["ticker"].nunique()
        for reason, n in counts.items():
            lines.append(f"- {reason}: {n} tickers")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_report.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add drifthunter/backtest/report.py tests/test_report.py
git commit -m "feat: pre-committed gate evaluation and markdown report"
```

---

### Task 14: CLI pipeline + determinism + end-to-end fixture test

**Files:**
- Create: `drifthunter/cli.py`
- Test: `tests/test_cli_e2e.py`

CLI commands (each stage writes parquet so reruns are cheap):
- `drifthunter signals` — ingest Form 4 quarters + 13D index/headers, apply scorers + exchange filter, write `data/signals.parquet`
- `drifthunter study` — fetch prices through the configured provider (cached), run event study, write `data/events.parquet` + coverage JSON
- `drifthunter report` — evaluate gates, write `data/report.md`, print verdicts

- [ ] **Step 1: Write the failing end-to-end test (fixtures only, no network)**

`tests/test_cli_e2e.py`:

```python
from datetime import date
from pathlib import Path

import pandas as pd

from drifthunter.config import load_config
from drifthunter.backtest.event_study import run_event_study
from drifthunter.backtest.report import evaluate_gate, render_markdown
from drifthunter.ingest.insider_datasets import load_quarter_dir
from drifthunter.scorer.form4 import detect_signals


class DictProvider:
    def __init__(self, frames):
        self.frames = frames

    def daily(self, ticker, start, end):
        return self.frames.get(ticker, pd.DataFrame())


def synthetic_prices(n=80, start="2024-02-01", price=10.0, vol=500_000.0):
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"open": price, "close": price, "volume": vol},
                        index=idx).rename_axis("date")


def test_pipeline_from_fixture_tsv_to_report(tmp_path):
    cfg = load_config(Path(__file__).parents[1] / "config.yaml")
    buys = load_quarter_dir(Path(__file__).parent / "fixtures" / "form345", cfg.form4)
    signals = detect_signals(buys, cfg.form4)
    assert len(signals) == 1  # fixture has a 2-insider EXM cluster

    frames = {"EXM": synthetic_prices(), "SPY": synthetic_prices(price=400.0, vol=1e7)}
    events = run_event_study(signals, DictProvider(frames), cfg.tradability,
                             horizons=cfg.study.horizons,
                             cost_bps_list=cfg.study.cost_bps_sweep,
                             benchmark=cfg.study.benchmark)
    assert not events.empty

    verdict = evaluate_gate(events, coverage_rate=1.0, profile="form4",
                            gate=cfg.gate, port=cfg.portfolio,
                            headline_cost_bps=cfg.study.headline_cost_bps,
                            horizons=cfg.study.horizons, seed=cfg.study.seed,
                            spy_annual_return=0.0)
    md = render_markdown([verdict], events)
    assert "Phase 0 Report" in md
    # Flat prices + costs => no edge: a real gate must kill this.
    assert verdict.decision == "KILL"


def test_study_is_deterministic():
    cfg = load_config(Path(__file__).parents[1] / "config.yaml")
    buys = load_quarter_dir(Path(__file__).parent / "fixtures" / "form345", cfg.form4)
    signals = detect_signals(buys, cfg.form4)
    frames = {"EXM": synthetic_prices(), "SPY": synthetic_prices(price=400.0, vol=1e7)}

    def run():
        return run_event_study(signals, DictProvider(frames), cfg.tradability,
                               horizons=cfg.study.horizons,
                               cost_bps_list=cfg.study.cost_bps_sweep,
                               benchmark=cfg.study.benchmark)

    pd.testing.assert_frame_equal(run(), run())  # spec §5: byte-identical
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_e2e.py -v`
Expected: FAIL (imports succeed but `signals`/event expectations may fail until CLI glue exists — if both tests already pass because all modules exist, that is acceptable; proceed to Step 3 for the CLI itself)

- [ ] **Step 3: Implement the CLI**

`drifthunter/cli.py`:

```python
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import click
import pandas as pd

from drifthunter.backtest.event_study import run_event_study
from drifthunter.backtest.report import evaluate_gate, render_markdown
from drifthunter.config import Config, load_config
from drifthunter.ingest.form_index import fetch_quarter
from drifthunter.ingest.header_parse import fetch_header
from drifthunter.ingest.http import EdgarClient
from drifthunter.ingest.insider_datasets import download_quarters
from drifthunter.ingest.tickers import TickerMap
from drifthunter.prices.free import FreeProvider
from drifthunter.prices.provider import CachingProvider, coverage_report
from drifthunter.prices.sharadar import SharadarProvider
from drifthunter.scorer import form4 as form4_scorer
from drifthunter.scorer import sc13d as sc13d_scorer
from drifthunter.scorer.models import Signal, ThirteenDFiling


def _provider(cfg: Config) -> CachingProvider:
    cache = cfg.data_dir / "prices"
    if cfg.prices.provider == "sharadar":
        key = os.environ.get(cfg.prices.nasdaq_api_key_env)
        if not key:
            raise click.ClickException(
                f"{cfg.prices.nasdaq_api_key_env} not set; either export it or "
                f"set prices.provider: free in config.yaml")
        return CachingProvider(SharadarProvider(key), cache)
    return CachingProvider(FreeProvider(), cache)


def _study_dates(cfg: Config) -> tuple[datetime, datetime]:
    return (datetime.fromisoformat(cfg.study.start),
            datetime.fromisoformat(cfg.study.end))


@click.group()
@click.option("--config", "config_path", default="config.yaml", type=Path)
@click.pass_context
def cli(ctx: click.Context, config_path: Path):
    ctx.obj = load_config(config_path)


@cli.command()
@click.pass_obj
def signals(cfg: Config):
    """Ingest filings, score them, apply exchange filter, write signals.parquet."""
    start, end = _study_dates(cfg)
    client = EdgarClient(cfg.edgar, cfg.data_dir / "cache")
    tickers = TickerMap.fetch(client)

    buys = download_quarters(client, start.date(), end.date(), cfg.form4)
    form4_signals = form4_scorer.detect_signals(buys, cfg.form4)
    click.echo(f"form4: {len(buys)} buys -> {len(form4_signals)} signals")

    filings: list[ThirteenDFiling] = []
    y, q = start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        for row in fetch_quarter(client, y, q):
            if not (start.date() <= row.date_filed <= end.date()):
                continue
            hdr = fetch_header(client, row.path)
            filings.append(ThirteenDFiling(
                accession=hdr.accession, subject_cik=hdr.subject_cik,
                subject_name=hdr.subject_name,
                ticker=tickers.ticker_for_cik(hdr.subject_cik) if hdr.subject_cik else None,
                filer_cik=hdr.filer_cik, filer_name=hdr.filer_name,
                filing_date=row.date_filed, is_amendment=False,
            ))
        q += 1
        if q == 5:
            y, q = y + 1, 1
    sc13d_signals = sc13d_scorer.detect_signals(filings, cfg.sc13d)
    click.echo(f"sc13d: {len(filings)} filings -> {len(sc13d_signals)} signals")

    all_signals = [
        s for s in form4_signals + sc13d_signals
        if _cik_listed(s, buys, filings, tickers, cfg)
    ]
    df = pd.DataFrame([{
        "profile": s.profile, "ticker": s.ticker,
        "trigger_date": pd.Timestamp(s.trigger_date),
        "score": s.score, "detail": s.detail,
    } for s in all_signals])
    out = cfg.data_dir / "signals.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out)
    click.echo(f"{len(df)} signals -> {out}")


def _cik_listed(sig: Signal, buys, filings, tickers: TickerMap, cfg: Config) -> bool:
    """Exchange filter (spec §3.3): signal ticker must map to an allowed exchange."""
    ciks = {b.ticker: b.issuer_cik for b in buys if b.ticker}
    ciks.update({f.ticker: f.subject_cik for f in filings if f.ticker})
    cik = ciks.get(sig.ticker)
    return bool(cik) and tickers.is_listed(cik, cfg.tradability.allowed_exchanges)


@cli.command()
@click.pass_obj
def study(cfg: Config):
    """Run the event study over signals.parquet; write events.parquet + coverage.json."""
    sig_df = pd.read_parquet(cfg.data_dir / "signals.parquet")
    sigs = [Signal(profile=r.profile, ticker=r.ticker,
                   trigger_date=r.trigger_date.date(), score=r.score, detail=r.detail)
            for r in sig_df.itertuples(index=False)]
    provider = _provider(cfg)
    start, end = _study_dates(cfg)

    events = run_event_study(sigs, provider, cfg.tradability,
                             horizons=cfg.study.horizons,
                             cost_bps_list=cfg.study.cost_bps_sweep,
                             benchmark=cfg.study.benchmark)
    events.to_parquet(cfg.data_dir / "events.parquet")

    cov = {}
    for profile in sig_df["profile"].unique():
        tickers = sig_df[sig_df["profile"] == profile]["ticker"].tolist()
        report = coverage_report(tickers, provider, start.date(), end.date())
        cov[profile] = asdict(report) | {"rate": report.rate}
    (cfg.data_dir / "coverage.json").write_text(json.dumps(cov, indent=2))
    click.echo(f"{len(events)} event rows; coverage: "
               + ", ".join(f"{p}={c['rate']:.0%}" for p, c in cov.items()))


@cli.command()
@click.pass_obj
def report(cfg: Config):
    """Evaluate the §3.6 gate and write report.md."""
    events = pd.read_parquet(cfg.data_dir / "events.parquet")
    cov = json.loads((cfg.data_dir / "coverage.json").read_text())
    provider = _provider(cfg)
    start, end = _study_dates(cfg)
    spy = provider.daily(cfg.study.benchmark, start.date(), end.date())
    years = (end - start).days / 365.25
    spy_annual = ((float(spy["close"].iloc[-1]) / float(spy["close"].iloc[0]))
                  ** (1 / years) - 1) if not spy.empty else 0.0

    verdicts = []
    for profile in ("form4", "sc13d"):
        if profile not in cov:
            continue
        verdicts.append(evaluate_gate(
            events, coverage_rate=cov[profile]["rate"], profile=profile,
            gate=cfg.gate, port=cfg.portfolio,
            headline_cost_bps=cfg.study.headline_cost_bps,
            horizons=cfg.study.horizons, seed=cfg.study.seed,
            spy_annual_return=spy_annual,
        ))
    md = render_markdown(verdicts, events)
    out = cfg.data_dir / "report.md"
    out.write_text(md, encoding="utf-8")
    for v in verdicts:
        click.echo(f"{v.profile}: {v.decision} ({'; '.join(v.reasons)})")
    click.echo(f"report -> {out}")
```

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: ALL PASS (~40 tests)

- [ ] **Step 5: Commit**

```bash
git add drifthunter/cli.py tests/test_cli_e2e.py
git commit -m "feat: CLI pipeline (signals -> study -> report) with e2e and determinism tests"
```

---

### Task 15: Real run (operational — produces the actual Phase 0 verdict)

**Files:**
- Create: `docs/phase0-runbook.md`
- Modify: `config.yaml` (only `sc13d.activist_ciks` curation; gate values are frozen)

No code in this task. It executes the validated pipeline on real data.

- [ ] **Step 1: Curate the activist list**

Add 10–20 well-known activist filer CIKs to `sc13d.activist_ciks` in `config.yaml` (look up CIKs at https://www.sec.gov/cgi-bin/browse-edgar by fund name: Elliott, Starboard, ValueAct, Icahn, Third Point, Pershing Square, Engaged, Ancora, JANA, Sarissa, etc.). Commit with the source of each CIK in the commit message.

- [ ] **Step 2: Set up price access**

Either subscribe to Sharadar SEP (Nasdaq Data Link, ~$40/mo, spec §3.1) and `export NASDAQ_DATA_LINK_API_KEY=...`, or set `prices.provider: free` in config to use the fallback (expect degraded coverage; the report will mark verdicts SUSPECT if coverage < 80%).

- [ ] **Step 3: Run the pipeline**

```bash
uv run drifthunter signals    # ~30-60 min first run (EDGAR rate limit on 13D headers)
uv run drifthunter study      # bulk price fetches, cached to data/prices/
uv run drifthunter report
```

Expected: three commands complete; `data/report.md` contains a verdict per profile.

- [ ] **Step 4: Write the runbook + archive the verdict**

Create `docs/phase0-runbook.md` recording: run date, dataset quarters used, provider used, coverage rates, verdicts with the §3.6 checklist, and the decision (which profile(s), which horizon, or KILL). Commit `data/report.md` alongside it (the parquet/cache stay gitignored).

```bash
git add docs/phase0-runbook.md data/report.md config.yaml
git commit -m "docs: Phase 0 run results and verdict"
```

- [ ] **Step 5: Act on the verdict**

- **GO (either profile):** stop here. The live system (spec §4) gets its own brainstorm-reviewed plan.
- **KILL (both):** stop here permanently, per spec §3.6. The repo remains a research tool.
- **SUSPECT-*:** fix coverage (paid provider month) and re-run before treating the verdict as real.

---

## Self-review notes

- **Spec coverage:** §3.1 ingest (Tasks 5–7, 9), §3.2 coverage/bias (Tasks 8, 13, 15), §3.3 scorer + filters (Tasks 3, 4, 10, exchange filter in 14), §3.4 event study (Task 10), §3.5 portfolio sim (Task 12), §3.6 gate (Task 13), §5 determinism + golden fixtures (Tasks 6, 7, 14). Live-system sections (§4) deliberately out of scope per the scope guard.
- **Known real-world risks flagged in-plan:** SEC TSV column names (Task 6 Step 6 verifies against a real quarter), form.idx column drift (parser splits on whitespace runs, not fixed offsets), Stooq symbol mismatches (coverage report quantifies).
- **Type consistency:** `Signal`/`InsiderBuy`/`ThirteenDFiling` defined in Task 2 and used with identical fields in Tasks 3, 4, 10, 14; `PriceProvider.daily(ticker, start, end) -> DataFrame[open, close, volume]` consistent across Tasks 8, 9, 10, 14; `filter_reason` values (`""`, `no_prices`, `min_price`, `adv`, `insufficient_history`, `benchmark_gap`) produced in Task 10 and consumed in Tasks 12–14.
