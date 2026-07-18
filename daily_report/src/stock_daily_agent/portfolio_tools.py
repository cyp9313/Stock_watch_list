# -*- coding: utf-8 -*-
"""Portfolio Agent 工具（Qwen-Agent BaseTool）。

工具让 Agent 读取确定性 Portfolio 数据、风险发现、新闻证据，并把最终
结构化建议写回磁盘。所有 ``call`` 返回 JSON 字符串，便于模型解析与重试。

对应修改计划 6.3：ReadPortfolioSnapshot / Metrics / RiskFindings /
RiskRanking / Evidence / InspectRunState + SavePortfolioAdvice。
"""
from __future__ import annotations

import json
from typing import Any

from qwen_agent.tools import BaseTool

from .portfolio_context import PortfolioRunContext
from .portfolio_schema import normalize_advice
from portfolio_analysis.validators import validate_portfolio_advice, PortfolioAdviceValidationError


_CTX: PortfolioRunContext | None = None


def set_portfolio_context(ctx: PortfolioRunContext) -> None:
    global _CTX
    _CTX = ctx


def get_portfolio_context() -> PortfolioRunContext:
    if _CTX is None:
        raise RuntimeError("Portfolio context 未设置；请先调用 set_portfolio_context。")
    return _CTX


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _parse_params(params: Any) -> dict:
    if isinstance(params, dict):
        return params
    if isinstance(params, str):
        text = params.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # 容忍裸 JSON 被 markdown 包裹
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except (json.JSONDecodeError, ValueError):
                    return {}
    return {}


def _compact_holdings(ctx: PortfolioRunContext) -> list[dict]:
    rc = ctx.rc_by_ticker()
    meta = ctx.meta_by_ticker()
    out = []
    for h in ctx.holdings():
        t = h["ticker"]
        m = meta.get(t, {})
        out.append({
            "ticker": t,
            "name": h.get("name") or t,
            "instrument_type": m.get("instrument_type"),
            "account_group": m.get("account_group") or h.get("group"),
            "sector": m.get("sector"),
            "theme": m.get("theme"),
            "underlying_index": m.get("underlying_index"),
            "weight": round(float(h.get("weight") or 0.0), 4),
            "profit_loss_pct": h.get("profit_loss_pct"),
            "return_1d": h.get("return_1d"), "return_5d": h.get("return_5d"),
            "return_1m": h.get("return_1m"), "return_ytd": h.get("return_ytd"),
            "price_vs_ema20_pct": h.get("price_vs_ema20_pct"), "price_vs_ema50_pct": h.get("price_vs_ema50_pct"), "price_vs_ema200_pct": h.get("price_vs_ema200_pct"),
            "rsi": h.get("rsi"), "rsi_regime": h.get("rsi_regime"), "volume_ratio": h.get("volume_ratio"), "beta": h.get("beta"),
            "annualized_volatility": (ctx.metrics.get("holdings_detail", {}) or {}).get(t, {}).get("annualized_volatility"),
            "max_drawdown_63d": (ctx.metrics.get("holdings_detail", {}) or {}).get(t, {}).get("max_drawdown_63d"),
            "risk_contribution": (rc.get(t) or {}).get("risk_contribution"),
            "risk_weight_gap": (rc.get(t) or {}).get("risk_weight_gap"),
            "risk_priority_score": (ctx.risk_by_ticker().get(t) or {}).get("risk_priority_score"),
        })
    return out


class ReadPortfolioSnapshotTool(BaseTool):
    name = "read_portfolio_snapshot"
    description = "读取 Portfolio 快照：基础货币、基准、总市值、盈亏、持仓数量、账户分组权重等概览数据。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_portfolio_context()
        snap = ctx.snapshot
        summary = snap.get("summary", {})
        top_groups = {}
        for h in ctx.holdings():
            g = (ctx.meta_by_ticker().get(h["ticker"], {}).get("account_group") or h.get("group") or "Portfolio")
            top_groups[g] = top_groups.get(g, 0.0) + float(h.get("weight") or 0.0)
        return _json({
            "portfolio_name": snap.get("portfolio_name"),
            "base_currency": snap.get("base_currency"),
            "benchmark": snap.get("benchmark"),
            "as_of": snap.get("as_of"),
            "total_market_value_base": summary.get("total_market_value_base"),
            "total_cost_basis_base": summary.get("total_cost_basis_base"),
            "profit_loss_base": summary.get("profit_loss_base"),
            "profit_loss_pct": summary.get("profit_loss_pct"),
            "holdings_count": len(ctx.holdings()),
            "account_group_weights": {k: round(v, 4) for k, v in sorted(top_groups.items(), key=lambda x: -x[1])},
        })


class ReadPortfolioMetricsTool(BaseTool):
    name = "read_portfolio_metrics"
    description = "读取 Portfolio 量化指标：Top1/Top3 权重、HHI、有效持仓数、Beta、年化波动率、63D/252D 最大回撤、相对收益、相关性、风险贡献。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_portfolio_context()
        m = ctx.metrics
        return _json({
            "top1_weight": m.get("top1_weight"),
            "top3_weight": m.get("top3_weight"),
            "hhi_10000": m.get("hhi_10000"),
            "effective_holdings": m.get("effective_holdings"),
            "portfolio_beta": m.get("portfolio_beta"),
            "annualized_volatility": m.get("annualized_volatility"),
            "max_drawdown_63d": m.get("max_drawdown_63d"),
            "max_drawdown_252d": m.get("max_drawdown_252d"),
            "relative_returns": m.get("relative_returns"),
            "average_pairwise_correlation": m.get("average_pairwise_correlation"),
            "max_pairwise_correlation": m.get("max_pairwise_correlation"),
            "high_correlation_pairs": m.get("high_correlation_pairs"),
            "technical_breadth": m.get("technical_breadth"),
            "risk_contributions": m.get("risk_contributions"),
            "aggregates": m.get("aggregates"),
            "portfolio_risk_score": m.get("portfolio_risk_score"),
            "portfolio_risk_level": m.get("portfolio_risk_level"),
            "risk_score_components": m.get("risk_score_components"),
            "holdings_detail": m.get("holdings_detail"),
        })


class ReadPortfolioRiskFindingsTool(BaseTool):
    name = "read_portfolio_risk_findings"
    description = "读取由 Python 确定性规则生成的 Portfolio 风险发现（集中度、风险贡献、高 Beta、技术面广度、回撤、相关性、重复暴露、数据质量）。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_portfolio_context()
        from portfolio_analysis.rules import generate_portfolio_rule_findings
        findings = generate_portfolio_rule_findings(
            ctx.snapshot, ctx.metrics, ctx.settings, instrument_metadata=ctx.instrument_metadata
        )
        return _json({"risk_findings": findings})


class ReadPortfolioRiskRankingTool(BaseTool):
    name = "read_portfolio_risk_ranking"
    description = "读取风险优先级排名：每个持仓的风险优先级分数、权重、风险贡献、技术风险分数，以及 Top-risk ticker 列表。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_portfolio_context()
        return _json({
            "items": ctx.ranking.get("items", []),
            "top_risk_tickers": ctx.ranking.get("top_risk_tickers", []),
            "research_ticker_count": ctx.ranking.get("research_ticker_count"),
        })


class ReadPortfolioEvidenceTool(BaseTool):
    name = "read_portfolio_evidence"
    description = "读取经过筛选、去重、来源分级与正文抓取后的结构化新闻证据（Evidence Notes）。每条含事件事实、中文摘要、影响方向与范围、关联 ticker。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_portfolio_context()
        return _json({"evidence_count": len(ctx.evidence), "evidence": ctx.evidence})


class InspectPortfolioRunStateTool(BaseTool):
    name = "inspect_portfolio_run_state"
    description = "查看当前运行态：是否已写入 advice JSON、输出 HTML 路径、上下文关键字段是否就绪。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_portfolio_context()
        advice_written = bool(ctx.advice_json_path and ctx.advice_json_path.exists())
        return _json({
            "advice_json_written": advice_written,
            "output_html": str(ctx.output_html) if ctx.output_html else None,
            "holdings_count": len(ctx.holdings()),
            "evidence_count": len(ctx.evidence),
            "top_risk_tickers": ctx.ranking.get("top_risk_tickers", []),
        })


class SavePortfolioAdviceTool(BaseTool):
    name = "save_portfolio_advice"
    description = (
        "保存并校验最终 Portfolio 建议 JSON（必须严格符合 schema）。"
        "校验会检查 action 与目标权重区间是否一致、ticker 是否属于组合、evidence_id 是否存在。"
        "若不一致，返回 ok=false 与 errors，请用修正后的 JSON 再次调用本工具。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "advice": {
                "type": "object",
                "description": "完整的 Portfolio 建议 JSON（见 schema 指南）。",
            }
        },
        "required": ["advice"],
    }

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_portfolio_context()
        data = _parse_params(params)
        raw = data.get("advice")
        if not isinstance(raw, dict):
            return _json({"ok": False, "errors": ["advice 字段缺失或不是对象。"]})
        try:
            normalized = normalize_advice(raw, snapshot=ctx.snapshot, metrics=ctx.metrics, ranking=ctx.ranking)
            validated = validate_portfolio_advice(
                normalized, ctx.snapshot, ctx.evidence, mode="strict"
            )
        except PortfolioAdviceValidationError as exc:
            return _json({"ok": False, "errors": exc.errors})
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "errors": [f"校验异常：{exc}"]})

        if ctx.advice_json_path is not None:
            ctx.advice_json_path.write_text(
                json.dumps(validated, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        ctx._saved_advice = validated  # type: ignore[attr-defined]
        return _json({"ok": True, "action_count": len(validated.get("actions", [])), "risk_count": len(validated.get("key_risks", []))})


def build_portfolio_tools() -> list[BaseTool]:
    return [
        ReadPortfolioSnapshotTool(),
        ReadPortfolioMetricsTool(),
        ReadPortfolioRiskFindingsTool(),
        ReadPortfolioRiskRankingTool(),
        ReadPortfolioEvidenceTool(),
        InspectPortfolioRunStateTool(),
        SavePortfolioAdviceTool(),
    ]
