# -*- coding: utf-8 -*-
"""Deterministic fallback for Portfolio AI Analyst v3.

This module contains no legacy agent schema or evidence-gate logic. It only
builds a renderable, observation-only report when the single Analyst call is
unavailable or returns unusable JSON.
"""
from __future__ import annotations

from typing import Any


def _weight_map(snapshot: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for holding in snapshot.get("holdings", []) or []:
        ticker = str(holding.get("ticker") or "").upper()
        if not ticker:
            continue
        try:
            result[ticker] = max(0.0, float(holding.get("weight") or 0.0))
        except (TypeError, ValueError):
            result[ticker] = 0.0
    return result


def build_quantitative_fallback(
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Build a non-directional fallback report from deterministic data only."""
    weights = _weight_map(snapshot)
    top_tickers = [
        str(ticker).upper()
        for ticker in (ranking.get("top_risk_tickers") or [])
        if str(ticker).upper() in weights
    ]
    actions: list[dict[str, Any]] = []
    for priority, ticker in enumerate(top_tickers[:6], start=1):
        current = weights[ticker]
        actions.append({
            "ticker": ticker,
            "action": "watch",
            "priority": priority,
            "action_timing": "monitor",
            "current_weight": current,
            "target_weight_min": current,
            "target_weight_max": current,
            "confidence": 0.40,
            "portfolio_reason": "AI 分析未完成；本条仅按 Python 风险排序保留为观察项。",
            "technical_reason": "请结合报告中的确定性技术快照继续观察。",
            "news_reason": "本次未生成可用的 AI 消息面分析。",
            "bull_case": "技术趋势与基本面催化同时改善。",
            "bear_case": "风险贡献继续上升且技术趋势进一步恶化。",
            "execute_if": [],
            "cancel_or_upgrade_if": ["成功生成新的 Portfolio AI Analyst 报告后重新评估。"],
            "further_reduce_if": [],
            "monitoring_items": ["风险贡献、EMA20/EMA50、回撤和最新公司公告"],
            "evidence_ids": [],
            "metric_evidence": [],
            "expected_portfolio_risk_reduction": None,
        })

    risk_score = metrics.get("portfolio_risk_score")
    score_text = f"Python 组合风险评分为 {risk_score}。" if risk_score is not None else "Python 风险指标已保留。"
    return {
        "language": "zh-CN",
        "report_mode": "quantitative_fallback",
        "ai_analysis_available": False,
        "observation_only": True,
        "portfolio_stance": "observe",
        "risk_level": "medium",
        "confidence": 0.40,
        "final_confidence": 0.40,
        "executive_summary": [
            "本报告为量化降级报告，AI 综合分析未完成。",
            score_text,
            reason or "请检查 DashScope 配置或模型最终 JSON 后重新生成。",
        ],
        "portfolio_analysis": {
            "trend_view": "本次仅保留 Python 计算的技术快照，不生成模型趋势判断。",
            "concentration_view": "请参阅权重、HHI 与风险贡献图表。",
            "risk_view": "请参阅 Python 风险评分、回撤和波动率指标。",
            "relative_performance_view": "请参阅相对基准收益表。",
            "news_view": "本次未生成 AI 消息面分析。",
        },
        "key_risks": [],
        "actions": actions,
        "portfolio_reallocation": {
            "estimated_weight_reduction": 0.0,
            "destination": "none",
            "note": "量化降级报告不生成方向性再平衡方案。",
        },
        "watch_items": [],
        "data_limitations": [reason] if reason else [],
        "disclaimer": "本报告为量化降级报告，仅供研究参考，不构成投资建议。",
    }
