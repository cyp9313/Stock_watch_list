# -*- coding: utf-8 -*-
"""Portfolio Agent 运行上下文。

集中保存 Agent 运行时需要的确定性数据（snapshot / metrics / ranking /
evidence / instrument metadata），供 Agent tools 读取，也供 runner 在
Agent 完成后取回结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PortfolioRunContext:
    run_dir: Path
    portfolio_name: str = "Portfolio"
    portfolio_id: str = ""
    owner_scope: str = ""
    base_currency: str = "EUR"
    benchmark: str = "^GSPC"
    model: str = ""
    provider: str = "dashscope"
    search_provider: str = "auto"

    snapshot: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    ranking: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    instrument_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    # 最终 HTML 输出路径
    output_html: Path | None = None
    # AI 写入的 advice JSON 路径
    advice_json_path: Path | None = None

    @property
    def safe_name(self) -> str:
        import re
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", self.portfolio_name or "Portfolio").strip("_") or "Portfolio"

    def holdings(self) -> list[dict[str, Any]]:
        return self.snapshot.get("holdings", []) or []

    def weights(self) -> dict[str, float]:
        return {h["ticker"]: float(h.get("weight") or 0.0) for h in self.holdings()}

    def risk_by_ticker(self) -> dict[str, dict[str, Any]]:
        return {item.get("ticker"): item for item in self.ranking.get("items", []) or []}

    def rc_by_ticker(self) -> dict[str, dict[str, Any]]:
        return {item.get("ticker"): item for item in self.metrics.get("risk_contributions", []) or []}

    def meta_by_ticker(self) -> dict[str, dict[str, Any]]:
        return self.instrument_metadata
