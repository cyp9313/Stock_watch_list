"""Deterministic final stage for evidence-backed AI Stock Reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .config import RunContext
from .decision_context import build_decision_context
from .decision_guardrails import apply_guardrails
from .decision_schema import DecisionDashboard, EvidenceBackedItem, PhaseDecision, PositionAdvice, TradingLevels
from .report_integrity import IntegrityResult, validate_decision_integrity


@dataclass(frozen=True)
class FinalizationResult:
    ok: bool
    decision_file: Path | None
    output_html: Path | None
    fallback_used: bool
    errors: list[str]
    warnings: list[str]


def minimum_artifacts_exist(ctx: RunContext) -> bool:
    return ctx.data_file.is_file() and ctx.chart_file.is_file() and (ctx.final_notes_json_file.is_file() or ctx.notes_file.is_file())


def _rating_class(context: dict[str, Any]) -> str:
    value = str(context["authoritative_rating"].get("rating_class") or "").lower()
    if value in {"buy", "hold", "avoid"}:
        return value
    score = float(context["authoritative_rating"]["final_score"])
    return "buy" if score >= 65 else "avoid" if score < 40 else "hold"


def _fallback_action(rating_class: str, has_position: bool) -> str:
    if rating_class == "buy":
        return "hold" if has_position else "watch"
    if rating_class == "avoid":
        return "reduce" if has_position else "avoid"
    return "hold" if has_position else "watch"


def _evidence_items(context: dict[str, Any], tag: str) -> list[EvidenceBackedItem]:
    items: list[EvidenceBackedItem] = []
    for item in context.get("evidence_items", []):
        if str(item.get("tag") or "").upper() != tag:
            continue
        text = str(item.get("investment_meaning") or item.get("fact") or item.get("title") or "").strip()
        evidence_id = str(item.get("evidence_id") or "").strip()
        if text and evidence_id:
            items.append(EvidenceBackedItem(text=text[:1000], evidence_ids=[evidence_id]))
        if len(items) == 3:
            break
    return items


def build_fallback_decision(context: dict[str, Any], reason: str) -> DecisionDashboard:
    """Build a conservative decision card from already-validated artifacts."""
    rating_class = _rating_class(context)
    holding = context["holding_context"]
    has_position = bool(holding["has_position"])
    action = _fallback_action(rating_class, has_position)
    levels = context["level_candidates"]
    display = levels.get("display") or {}
    support_near = bool(levels.get("ideal_buy_candidate")) and any(
        float(item.get("distance_pct") or 999) <= 3 for item in levels.get("supports", []) if isinstance(item, dict)
    )
    if rating_class == "buy" and support_near:
        conclusion = "综合评分偏多且价格接近有效支撑；等待止跌或量能确认后再评估行动。"
    elif rating_class == "buy":
        conclusion = "综合评分偏多，但当前价格未接近主要支撑，暂不宜追高。"
    elif rating_class == "avoid":
        conclusion = "风险和弱势因素占优，当前应优先控制风险而非寻找进场机会。"
    else:
        conclusion = "多空因素暂未形成一致方向，当前以观察和条件确认优先。"
    stop = display.get("stop_loss")
    invalidation = f"若价格有效跌破参考止损区间 {stop}，重新评估并执行风险控制。" if stop else "若关键支撑失守或技术结构明显恶化，重新评估并执行风险控制。"
    if rating_class == "buy":
        no_position = "等待价格在参考支撑附近企稳并获得确认后，再考虑分批观察。"
        has_position_advice = "以持有为主；仅在确认条件满足且组合集中度可控时再考虑加仓。"
    elif rating_class == "avoid":
        no_position = "暂时回避，优先等待风险因素缓解和结构修复。"
        has_position_advice = "优先降低风险；若失效条件触发，按既定纪律减仓或止损。"
    else:
        no_position = "保持观察，等待趋势、量能或催化因素提供更清晰确认。"
        has_position_advice = "持有或适度降低风险，重点关注失效条件与下一次关键事件。"
    phase = context["market_session"]
    catalysts = _evidence_items(context, "BULL")
    risks = _evidence_items(context, "BEAR")
    return DecisionDashboard(
        one_sentence=conclusion,
        proposed_action=action,
        final_action=action,
        final_score=float(context["authoritative_rating"]["final_score"]),
        confidence="medium" if context["data_quality"]["level"] == "good" else "low",
        position_advice=PositionAdvice(no_position=no_position, has_position=has_position_advice),
        levels=TradingLevels(
            ideal_buy=display.get("ideal_buy"), secondary_buy=display.get("secondary_buy"),
            stop_loss=stop, take_profit=display.get("take_profit"), invalidation_condition=invalidation,
        ),
        catalysts=catalysts,
        risk_alerts=risks,
        action_checklist=[
            "观察价格是否在主要支撑附近企稳。",
            "观察成交量是否确认突破或跌破。",
            "检查下一次财报或重大事件前后的风险。",
            "若触发失效条件，执行既定风险控制。",
        ],
        phase_decision=PhaseDecision(
            market=str(phase["market"]), timezone=str(phase["timezone"]), phase=str(phase["phase"]),
            action_window="等待下一个可验证的市场阶段或价格确认。",
            immediate_action="当前使用确定性保守决策；不要把参考点位视为保证成交价格。",
            watch_conditions=["支撑企稳或阻力突破需结合成交量确认。"],
            data_limitations=list(context["data_quality"].get("limitations") or []),
        ),
        score_explanation="最终评分由现有 Python 多因子逻辑计算；本决策层仅解释评分与证据，不会重算或覆盖分数。",
        fallback_used=True,
        fallback_reason=reason,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _render_html(ctx: RunContext) -> tuple[bool, str]:
    args = [
        sys.executable, str(ctx.paths.scripts_dir / "build_report.py"), str(ctx.data_file), str(ctx.chart_file),
        str(ctx.final_output_html), "--date", ctx.report_date, "--months", str(ctx.months), "--decision-json", str(ctx.decision_file),
    ]
    if ctx.notes_file.is_file():
        args.extend(["--notes", str(ctx.notes_file)])
    if ctx.final_notes_json_file.is_file():
        args.extend(["--evidence", str(ctx.final_notes_json_file)])
    completed = subprocess.run(args, cwd=ctx.run_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if completed.returncode != 0 or not ctx.final_output_html.is_file() or ctx.final_output_html.stat().st_size == 0:
        return False, (completed.stderr or completed.stdout)[-1600:]
    return True, ""


def finalize_report(
    ctx: RunContext,
    *,
    holding_context: dict[str, Any] | None = None,
    proposal: DecisionDashboard | None = None,
    synthesis_meta: dict[str, Any] | None = None,
    decision_provider: str | None = None,
    decision_model: str | None = None,
) -> FinalizationResult:
    """Finalize the report from local artifacts; never fetch market or news data."""
    if not minimum_artifacts_exist(ctx):
        return FinalizationResult(False, None, None, False, ["minimum report artifacts are missing"], [])
    warnings: list[str] = []
    try:
        context = build_decision_context(ctx, holding_context=holding_context)
    except Exception as exc:
        return FinalizationResult(False, None, None, False, [f"cannot build decision context: {exc}"], [])
    _write_json(ctx.decision_context_file, context)
    decision = proposal
    meta = synthesis_meta
    if decision is None and os.environ.get("DECISION_REPORT_ENABLED", "true").strip().lower() not in {"0", "false", "no"}:
        from .decision_synthesizer import synthesize_decision
        synthesis = synthesize_decision(context, provider=decision_provider, model=decision_model)
        meta = {
            "attempted": True, "ok": synthesis.ok, "provider": synthesis.provider, "model": synthesis.model,
            "elapsed_seconds": round(synthesis.elapsed_seconds, 3), "error": synthesis.error,
        }
        decision = synthesis.proposal
    fallback_reason: str | None = None
    if decision is None:
        fallback_reason = "Decision synthesis was disabled or unavailable; deterministic fallback was used."
        decision = build_fallback_decision(context, fallback_reason)
    integrity = validate_decision_integrity(decision, context)
    if "authoritative_score_override" in integrity.errors:
        payload = decision.model_dump()
        payload["final_score"] = context["authoritative_rating"]["final_score"]
        decision = DecisionDashboard.model_validate(payload)
        integrity = validate_decision_integrity(decision, context)
    if not integrity.ok and not decision.fallback_used:
        fallback_reason = "Decision proposal failed integrity validation; deterministic fallback was used."
        decision = build_fallback_decision(context, fallback_reason)
        integrity = validate_decision_integrity(decision, context)
    decision = apply_guardrails(decision, context, integrity)
    _write_json(ctx.decision_file, decision.model_dump())
    meta = meta or {"attempted": False, "ok": False, "provider": "", "model": "", "elapsed_seconds": 0, "error": None}
    audit = {
        "schema_version": "1.0", "ticker": ctx.ticker, "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision_enabled": os.environ.get("DECISION_REPORT_ENABLED", "true").strip().lower() not in {"0", "false", "no"},
        "synthesis": meta, "integrity": {"errors": integrity.errors, "warnings": integrity.warnings},
        "adjustments": [item.model_dump() for item in decision.adjustments],
        "authoritative_score": context["authoritative_rating"]["final_score"], "proposal_score": proposal.final_score if proposal else None,
        "final_score": decision.final_score, "fallback_used": decision.fallback_used, "fallback_reason": decision.fallback_reason,
    }
    _write_json(ctx.finalization_audit_file, audit)
    ok, error = _render_html(ctx)
    if not os.environ.get("DECISION_REPORT_KEEP_CONTEXT", "false").strip().lower() in {"1", "true", "yes"}:
        ctx.decision_context_file.unlink(missing_ok=True)
    if not ok:
        return FinalizationResult(False, ctx.decision_file, None, decision.fallback_used, [f"report rendering failed: {error}"], integrity.warnings)
    warnings.extend(integrity.warnings)
    return FinalizationResult(True, ctx.decision_file, ctx.final_output_html, decision.fallback_used, [], warnings)
