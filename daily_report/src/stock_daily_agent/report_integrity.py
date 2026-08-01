"""Model-independent integrity checks for decision-dashboard artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .decision_schema import DecisionDashboard


@dataclass(frozen=True)
class IntegrityResult:
    ok: bool
    errors: list[str]
    warnings: list[str]


def _requires_chinese_text(value: object) -> bool:
    """Narrative fields must be Chinese; tickers, prices and IDs are exempt."""
    text = str(value or "").strip()
    return bool(text) and any("A" <= char <= "z" for char in text) and not any("\u4e00" <= char <= "\u9fff" for char in text)


def _stop_range(context: dict[str, Any]) -> tuple[float, float] | None:
    value = str(((context.get("level_candidates") or {}).get("display") or {}).get("stop_loss") or "")
    numbers = [float(item.replace(",", "")) for item in re.findall(r"\d[\d,]*(?:\.\d+)?", value)]
    if len(numbers) < 2:
        return None
    return min(numbers[0], numbers[1]), max(numbers[0], numbers[1])


def _has_conflicting_risk_boundary(value: object, context: dict[str, Any]) -> bool:
    stop_range = _stop_range(context)
    if not stop_range:
        return False
    lower, upper = stop_range
    text = str(value or "")
    for clause in re.split(r"[。；;\n]", text):
        if not any(token in clause for token in ("止损", "跌破", "减仓", "失效")):
            continue
        prices = [float(item.replace(",", "")) for item in re.findall(r"[$￥¥]\s*(\d[\d,]*(?:\.\d+)?)", clause)]
        if any(price < lower or price > upper for price in prices):
            return True
    return False


def validate_decision_integrity(decision: DecisionDashboard, context: dict[str, Any]) -> IntegrityResult:
    """Validate evidence, score, action completeness and Python-owned session facts."""
    errors: list[str] = []
    warnings: list[str] = []
    authoritative = float(context["authoritative_rating"]["final_score"])
    if abs(decision.final_score - authoritative) > 1e-6:
        errors.append("authoritative_score_override")
    allowed = {str(value).upper() for value in context.get("allowed_evidence_ids", [])}
    for label, items in (("catalyst", decision.catalysts), ("risk", decision.risk_alerts)):
        for item in items:
            for evidence_id in item.evidence_ids:
                if not str(evidence_id).strip() or str(evidence_id).upper() not in allowed:
                    errors.append(f"invalid_{label}_evidence_id:{evidence_id}")
    allowed_levels = {
        str(value)
        for value in (context.get("level_candidates", {}).get("display") or {}).values()
        if value
    }
    for label in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit"):
        value = getattr(decision.levels, label)
        if value is not None and value not in allowed_levels:
            errors.append(f"unrecognized_level:{label}")
    if len(decision.action_checklist) < 2:
        errors.append("action_checklist_requires_at_least_two_items")
    if decision.proposed_action in {"buy", "add"} and not (decision.levels.stop_loss or decision.levels.invalidation_condition):
        errors.append("positive_action_requires_risk_boundary")
    if decision.proposed_action in {"buy", "add"} and not decision.catalysts:
        warnings.append("positive_action_has_no_evidence_backed_catalyst")
    session = context["market_session"]
    phase = decision.phase_decision
    for field in ("market", "timezone", "phase"):
        if getattr(phase, field) != session.get(field):
            errors.append(f"market_session_mismatch:{field}")
    if len(decision.catalysts) > 5 or len(decision.risk_alerts) > 5:
        errors.append("too_many_evidence_items")
    chinese_fields = [
        decision.one_sentence,
        decision.position_advice.no_position,
        decision.position_advice.has_position,
        decision.levels.invalidation_condition,
        decision.phase_decision.action_window,
        decision.phase_decision.immediate_action,
        decision.score_explanation,
        *decision.action_checklist,
        *(item.text for item in decision.catalysts),
        *(item.text for item in decision.risk_alerts),
    ]
    if any(_requires_chinese_text(value) for value in chinese_fields):
        errors.append("decision_narrative_must_be_chinese")
    risk_boundary_fields = {
        "position_advice.no_position": decision.position_advice.no_position,
        "position_advice.has_position": decision.position_advice.has_position,
        "action_checklist": "\n".join(decision.action_checklist),
        "phase_decision.immediate_action": decision.phase_decision.immediate_action,
    }
    for field, value in risk_boundary_fields.items():
        if _has_conflicting_risk_boundary(value, context):
            errors.append(f"risk_boundary_conflicts_with_stop_reference:{field}")
    return IntegrityResult(ok=not errors, errors=errors, warnings=warnings)
