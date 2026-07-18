from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PortfolioReportSettings:
    base_currency: str = "EUR"
    benchmark: str = "^GSPC"
    investment_horizon: str = "1-3m"
    risk_profile: str = "balanced"
    max_position_pct: float = 20.0
    max_group_pct: float = 40.0
    allow_add: bool = True
    allow_reduce: bool = True
    research_max_tickers: int = 5

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "PortfolioReportSettings":
        data = dict(value or {})
        return cls(
            base_currency=str(data.get("base_currency") or "EUR").upper(),
            benchmark=str(data.get("benchmark") or "^GSPC").upper(),
            investment_horizon=str(data.get("investment_horizon") or "1-3m"),
            risk_profile=str(data.get("risk_profile") or "balanced"),
            max_position_pct=float(data.get("max_position_pct", 20.0) or 20.0),
            max_group_pct=float(data.get("max_group_pct", 40.0) or 40.0),
            allow_add=bool(data.get("allow_add", True)),
            allow_reduce=bool(data.get("allow_reduce", True)),
            research_max_tickers=int(data.get("research_max_tickers", 5) or 5),
        )


@dataclass
class PortfolioReportBundle:
    snapshot: dict[str, Any]
    metrics: dict[str, Any]
    risk_ranking: dict[str, Any]
    advice: dict[str, Any]
    evidence: list[dict[str, Any]] = field(default_factory=list)
