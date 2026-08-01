"""Small, explainable action guardrails for AI Stock Reports."""

from __future__ import annotations

from typing import Any

from .decision_schema import DecisionAdjustment, DecisionDashboard
from .report_integrity import IntegrityResult


RATING_CLASS_ALLOWED_ACTIONS = {
    "buy": {"buy", "add", "hold", "watch"},
    "hold": {"hold", "watch", "reduce"},
    "avoid": {"avoid", "reduce", "sell", "watch"},
}


def _append_adjustment(decision: DecisionDashboard, target: str, reason: str) -> DecisionDashboard:
    if decision.final_action == target:
        return decision
    payload = decision.model_dump()
    adjustments = list(payload["adjustments"])
    adjustments.append({"from_action": payload["final_action"], "to_action": target, "reason": reason})
    payload["final_action"] = target
    payload["adjustments"] = adjustments
    return DecisionDashboard.model_validate(payload)


def _rating_class(context: dict[str, Any]) -> str:
    rating = str(context.get("authoritative_rating", {}).get("rating_class") or "").lower()
    if rating in RATING_CLASS_ALLOWED_ACTIONS:
        return rating
    score = float(context.get("authoritative_rating", {}).get("final_score") or 50)
    return "buy" if score >= 65 else "avoid" if score < 40 else "hold"


def _near_support(context: dict[str, Any]) -> bool:
    price = context.get("instrument", {}).get("current_price")
    atr = context.get("technical", {}).get("atr14")
    supports = context.get("level_candidates", {}).get("supports") or []
    if not isinstance(price, (int, float)) or price <= 0 or not supports:
        return False
    support = supports[0].get("value") if isinstance(supports[0], dict) else None
    if not isinstance(support, (int, float)):
        return False
    threshold = max(0.03, float(atr or 0) / price)
    return abs(price - support) / price <= threshold


def apply_guardrails(
    decision: DecisionDashboard,
    context: dict[str, Any],
    integrity: IntegrityResult,
) -> DecisionDashboard:
    """Apply deterministic, audited reductions without changing the score."""
    result = decision
    holding = context.get("holding_context") or {}
    has_position = bool(holding.get("has_position"))
    quality = str((context.get("data_quality") or {}).get("level") or "poor")
    rating_class = _rating_class(context)
    score = float(context.get("authoritative_rating", {}).get("final_score") or 50)
    nontechnical_evidence = [item for item in context.get("evidence_items", []) if not str(item.get("evidence_id", "")).upper().startswith("TECH-")]

    # A sell recommendation for somebody without a position is semantically an
    # avoid recommendation, independent of the score-band wording.
    translated_no_position_sell = not has_position and result.final_action == "sell"
    if translated_no_position_sell:
        result = _append_adjustment(result, "avoid", "无持仓场景将卖出建议转换为回避。")

    allowed = RATING_CLASS_ALLOWED_ACTIONS[rating_class]
    if result.final_action not in allowed and not translated_no_position_sell:
        target = "watch" if rating_class != "avoid" else "avoid"
        result = _append_adjustment(result, target, f"行动与 Python 权威评级 {rating_class} 不一致，已降级。")

    if not has_position:
        no_position_targets = {"add": "buy", "reduce": "watch", "sell": "avoid", "hold": "watch"}
        if result.final_action in no_position_targets:
            result = _append_adjustment(result, no_position_targets[result.final_action], "无持仓场景不输出加仓、减仓、卖出或持有动作。")

    evidence_insufficient = quality == "poor" or not nontechnical_evidence or 40 <= score <= 60
    if evidence_insufficient:
        if result.final_action in {"buy", "add"}:
            result = _append_adjustment(result, "hold" if has_position else "watch", "证据或评分处于保守区间，禁止强买入动作。")
        elif result.final_action == "sell":
            result = _append_adjustment(result, "reduce" if has_position else "avoid", "证据不足以支持强卖出动作。")

    confirmation = _near_support(context) or float(context.get("technical", {}).get("technical_score") or 0) >= 65 or bool(result.catalysts)
    if result.final_action in {"buy", "add"} and not confirmation:
        result = _append_adjustment(result, "hold" if result.final_action == "add" and has_position else "watch", "缺少支撑确认、技术强势或有效催化证据。")

    if result.final_action == "sell" and not result.risk_alerts:
        result = _append_adjustment(result, "reduce" if has_position else "avoid", "缺少结构破坏或风险证据，强卖出已降级。")
    weight = holding.get("portfolio_weight")
    if result.final_action == "add" and isinstance(weight, (int, float)) and weight >= 0.25:
        result = _append_adjustment(result, "hold", "当前标的组合权重不低于 25%，加仓已降级为持有。")
    if not integrity.ok and result.final_action in {"buy", "add", "sell"}:
        result = _append_adjustment(result, "hold" if has_position else "watch", "决策完整性检查未通过，已采用保守动作。")
    return result
