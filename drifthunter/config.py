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
