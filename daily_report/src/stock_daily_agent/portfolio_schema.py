# -*- coding: utf-8 -*-
"""Portfolio Advice JSON Schema 与归一化。

对应修改计划 8 的 Advice JSON 结构。Agent 输出结构化 JSON，
Python 侧做类型/默认值归一化，再交给 validators 做动作-权重一致性校验。
"""
from __future__ import annotations

from typing import Any

from portfolio_analysis.validators import ALLOWED_ACTIONS

ALLOWED_RISK_LEVELS = {"low", "medium", "medium_high", "high"}
ALLOWED_SEVERITY = {"high", "medium", "low"}

# 修改计划第三轮 17：AI 只可讨论以下已计算/提供的指标；任何其他指标（如夏普比率、
# 隐含波动率、机构持仓变化、资金流向、流动性折价等）必须有 Evidence 明确支撑，否则禁止出现。
ALLOWED_METRIC_REGISTRY = {
    "weight", "risk_contribution", "risk_weight_gap", "top1_weight", "top3_weight",
    "hhi", "effective_holdings", "portfolio_beta", "annualized_volatility",
    "max_drawdown_63d", "max_drawdown_252d", "relative_return", "portfolio_return",
    "benchmark_return", "average_pairwise_correlation", "max_pairwise_correlation",
    "rsi", "rsi_regime", "price_vs_ema20_pct", "price_vs_ema50_pct", "price_vs_ema200_pct",
    "profit_loss_pct", "return_1d", "return_5d", "return_1m", "return_ytd",
    "distance_from_52w_high", "high_beta_weight", "below_ema50_weight",
    "top_risk_contribution_sum", "top_risk_weight_sum", "recommended_reduction_weight",
    "expected_portfolio_risk_reduction", "portfolio_risk_score", "portfolio_risk_level",
}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if __import__("math").isfinite(number) else default


def _as_str(value: Any, default: str = "") -> str:
    return "" if value is None else str(value)


def _normalize_reallocation(raw: Any) -> dict[str, Any]:
    """修改计划第三轮 29：释放资金的去向说明（系统无完整现金/目标配置时不自动推荐精确替代）。"""
    if not isinstance(raw, dict):
        return {
            "estimated_weight_reduction": None,
            "destination": "cash_unspecified",
            "note": "缺少现金余额和目标配置，暂不生成精确再分配方案。",
        }
    return {
        "estimated_weight_reduction": _as_float(raw.get("estimated_weight_reduction"), None),
        "destination": _as_str(raw.get("destination")) or "cash_unspecified",
        "note": _as_str(raw.get("note")) or "缺少现金余额和目标配置，暂不生成精确再分配方案。",
    }


def normalize_action(raw: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    ticker = str(raw.get("ticker") or "").upper()
    action = str(raw.get("action") or "watch").lower()
    if action not in ALLOWED_ACTIONS:
        action = "watch"
    current = weights.get(ticker, _as_float(raw.get("current_weight"), 0.0))
    # 修改计划第三轮 26：重构触发/失效条件语义。
    timing = str(raw.get("action_timing") or "act_now").lower()
    if timing not in {"act_now", "conditional", "monitor"}:
        timing = "act_now"
    thresholds = []
    for th in _as_list(raw.get("thresholds")):
        if isinstance(th, dict):
            thresholds.append({
                "metric": _as_str(th.get("metric")),
                "value": _as_float(th.get("value"), None),
                "basis": _as_str(th.get("basis")) or "scenario_assumption",
                "evidence_id": _as_str(th.get("evidence_id")) or None,
                "note": _as_str(th.get("note")),
            })
    metric_evidence = []
    for me in _as_list(raw.get("metric_evidence")):
        if isinstance(me, dict):
            metric_evidence.append({
                "metric": _as_str(me.get("metric")),
                "ticker": str(me.get("ticker") or ticker).upper(),
            })
    return {
        "ticker": ticker,
        "action": action,
        "action_zh": _as_str(raw.get("action_zh")) or None,
        "priority": int(raw.get("priority") or 0) or 0,
        "action_timing": timing,
        "current_weight": current,
        "target_weight_min": _as_float(raw.get("target_weight_min"), current),
        "target_weight_max": _as_float(raw.get("target_weight_max"), current),
        "confidence": max(0.0, min(1.0, _as_float(raw.get("confidence"), 0.5))),
        "portfolio_reason": _as_str(raw.get("portfolio_reason")),
        "technical_reason": _as_str(raw.get("technical_reason")),
        "news_reason": _as_str(raw.get("news_reason")),
        "bull_case": _as_str(raw.get("bull_case")),
        "bear_case": _as_str(raw.get("bear_case")),
        "execute_if": [str(x) for x in _as_list(raw.get("execute_if"))],
        "cancel_or_upgrade_if": [str(x) for x in _as_list(raw.get("cancel_or_upgrade_if"))],
        "further_reduce_if": [str(x) for x in _as_list(raw.get("further_reduce_if"))],
        "monitoring_items": [str(x) for x in _as_list(raw.get("monitoring_items"))],
        "thresholds": thresholds,
        "metric_evidence": metric_evidence,
        "expected_portfolio_risk_reduction": _as_float(raw.get("expected_portfolio_risk_reduction"), None),
        "evidence_ids": [str(x) for x in _as_list(raw.get("evidence_ids"))],
    }


def normalize_risk(raw: dict[str, Any]) -> dict[str, Any]:
    severity = str(raw.get("severity") or "medium").lower()
    if severity not in ALLOWED_SEVERITY:
        severity = "medium"
    return {
        "risk_id": _as_str(raw.get("risk_id")) or "R000",
        "title": _as_str(raw.get("title")) or "未命名风险",
        "severity": severity,
        "description": _as_str(raw.get("description")),
        "affected_tickers": [str(x) for x in _as_list(raw.get("affected_tickers"))],
        "metric_refs": [str(x) for x in _as_list(raw.get("metric_refs"))],
        "evidence_ids": [str(x) for x in _as_list(raw.get("evidence_ids"))],
    }


def normalize_advice(
    raw: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
) -> dict[str, Any]:
    """把模型（或兜底）输出的 advice 归一化为统一结构。"""
    weights = {h["ticker"]: float(h.get("weight") or 0.0) for h in snapshot.get("holdings", [])}
    risk_level = str(raw.get("risk_level") or "medium").lower()
    if risk_level not in ALLOWED_RISK_LEVELS:
        risk_level = "medium"

    actions = [normalize_action(a, weights) for a in _as_list(raw.get("actions"))]
    # 仅保留组合中存在的 ticker
    actions = [a for a in actions if a["ticker"] in weights]

    pa = raw.get("portfolio_analysis") or {}
    return {
        "language": _as_str(raw.get("language")) or "zh-CN",
        "report_mode": _as_str(raw.get("report_mode")) or "ai",
        "portfolio_stance": _as_str(raw.get("portfolio_stance")) or "balanced",
        "risk_level": risk_level,
        "confidence": max(0.0, min(1.0, _as_float(raw.get("confidence"), 0.5))),
        "executive_summary": [str(x) for x in _as_list(raw.get("executive_summary"))],
        "portfolio_analysis": {
            "trend_view": _as_str(pa.get("trend_view")),
            "concentration_view": _as_str(pa.get("concentration_view")),
            "risk_view": _as_str(pa.get("risk_view")),
            "relative_performance_view": _as_str(pa.get("relative_performance_view")),
            "news_view": _as_str(pa.get("news_view")),
        },
        "key_risks": [normalize_risk(r) for r in _as_list(raw.get("key_risks"))],
        "actions": actions,
        "portfolio_reallocation": _normalize_reallocation(raw.get("portfolio_reallocation")),
        "watch_items": [
            {
                "title": _as_str(w.get("title")),
                "reason": _as_str(w.get("reason")),
                "affected_tickers": [str(x) for x in _as_list(w.get("affected_tickers"))],
            }
            for w in _as_list(raw.get("watch_items"))
        ],
        "data_limitations": [str(x) for x in _as_list(raw.get("data_limitations"))],
        "disclaimer": _as_str(raw.get("disclaimer")) or "本报告仅供研究参考，不构成投资建议。",
    }


def default_fallback_advice(
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    *,
    reason: str = "",
) -> dict[str, Any]:
    """生成被明确标记为『量化降级报告』的兜底 advice（不含模型综合判断）。"""
    weights = {h["ticker"]: float(h.get("weight") or 0.0) for h in snapshot.get("holdings", [])}
    top_tickers = ranking.get("top_risk_tickers") or []
    actions = []
    for i, ticker in enumerate(top_tickers[:6], start=1):
        w = weights.get(ticker, 0.0)
        actions.append(normalize_action({
            "ticker": ticker,
            "action": "watch",
            "action_timing": "monitor",
            "priority": i,
            "current_weight": w,
            "target_weight_min": w,
            "target_weight_max": w,
            "confidence": 0.4,
            "portfolio_reason": "量化降级报告：未调用 AI，本条目仅基于确定性风险评分，不构成操作建议。",
            "technical_reason": "",
            "news_reason": "",
            "execute_if": [],
            "cancel_or_upgrade_if": ["获得 AI 综合分析后重写本条目。"],
            "further_reduce_if": ["如风险贡献或技术面进一步恶化，请结合 AI 报告复核。"],
            "monitoring_items": ["关注风险贡献与波动率变化。"],
        }, weights))
    return {
        "language": "zh-CN",
        "report_mode": "quantitative_fallback",
        "ai_analysis_available": False,
        "portfolio_stance": "balanced",
        "risk_level": "medium",
        "confidence": 0.4,
        "executive_summary": [
            "本报告为量化降级报告，AI 分析未完成。",
            "以下内容仅基于确定性指标，不包含模型综合判断。",
            reason or "请配置可用的 LLM（DashScope / DeepSeek / OpenAI-compatible）后重新生成 AI 报告。",
        ],
        "portfolio_analysis": {
            "trend_view": "（量化降级报告）未生成 AI 趋势判断。",
            "concentration_view": "",
            "risk_view": "",
            "relative_performance_view": "",
            "news_view": "",
        },
        "key_risks": [],
        "actions": actions,
        "watch_items": [],
        "data_limitations": [reason] if reason else [],
        "disclaimer": "本报告为量化降级报告，仅供研究参考，不构成投资建议。",
    }


# 提供给 Agent 的 schema 说明（中文），嵌入 system prompt。
ADVICE_SCHEMA_GUIDE = """\
输出必须是如下结构的 JSON（不要输出多余解释，只输出 JSON）：

{
  "language": "zh-CN",
  "report_mode": "ai",
  "portfolio_stance": "谨慎偏多 | 中性 | 谨慎偏空 | 防御",
  "risk_level": "low | medium | medium_high | high",
  "confidence": 0.0-1.0 的浮点,
  "executive_summary": ["3 条核心中文结论"],
  "portfolio_analysis": {
    "trend_view": "...", "concentration_view": "...", "risk_view": "...",
    "relative_performance_view": "...", "news_view": "..."
  },
  "key_risks": [
    {"risk_id":"R001","title":"...","severity":"high|medium|low",
     "description":"...","affected_tickers":["SOFI"],"metric_refs":["portfolio_beta"],"evidence_ids":["E005"]}
  ],
  "actions": [
    {"ticker":"SOFI","action":"trim|add|hold|reduce|exit|watch",
     "action_zh":"适度减仓","priority":1,"action_timing":"act_now|conditional|monitor",
     "current_weight":0.0646,"target_weight_min":0.045,"target_weight_max":0.055,
     "confidence":0.82,
     "portfolio_reason":"...","technical_reason":"...","news_reason":"...",
     "bull_case":"...","bear_case":"...",
     "execute_if":["当前建议已成立，立即执行"],"cancel_or_upgrade_if":["利好兑现则取消减仓"],
     "further_reduce_if":["风险贡献继续恶化则进一步减仓"],"monitoring_items":["关注..."],
     "thresholds":[{"metric":"uranium_price","value":90,"basis":"evidence|user_constraint|scenario_assumption","evidence_id":null,"note":"情景阈值，非市场一致预期"}],
     "metric_evidence":[{"metric":"risk_contribution","ticker":"SOFI"},{"metric":"weight","ticker":"SOFI"}],
     "expected_portfolio_risk_reduction":0.012,"evidence_ids":["E005"]}
  ],
  "portfolio_reallocation": {"estimated_weight_reduction":0.10,"destination":"cash_unspecified","note":"缺少现金余额和目标配置，暂不生成精确再分配方案。"},
  "watch_items": [{"title":"...","reason":"...","affected_tickers":["NVDA"]}],
  "data_limitations": ["..."],
  "disclaimer": "本报告仅供研究参考，不构成投资建议。"
}
"""
