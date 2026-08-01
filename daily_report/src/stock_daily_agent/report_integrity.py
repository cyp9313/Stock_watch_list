"""Model-independent integrity checks for decision-dashboard artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .decision_schema import DecisionDashboard


@dataclass(frozen=True)
class IntegrityResult:
    ok: bool
    errors: list[str]
    warnings: list[str]


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
    return IntegrityResult(ok=not errors, errors=errors, warnings=warnings)
