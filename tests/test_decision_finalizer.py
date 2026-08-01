"""Regression tests for decision fallback, integrity, and guardrails."""

from __future__ import annotations

import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "daily_report" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from stock_daily_agent.config import ProjectPaths, RunContext
from stock_daily_agent.decision_guardrails import apply_guardrails
from stock_daily_agent.decision_schema import DecisionDashboard, PhaseDecision, PositionAdvice, TradingLevels
from stock_daily_agent.finalizer import finalize_report
from stock_daily_agent.report_integrity import validate_decision_integrity


def _context(has_position: bool = False) -> dict:
    return {
        "instrument": {"current_price": 100},
        "authoritative_rating": {"final_score": 63.4, "rating_class": "hold"},
        "technical": {"technical_score": 55, "atr14": 2},
        "holding_context": {"has_position": has_position, "portfolio_weight": None},
        "data_quality": {"level": "good", "limitations": []},
        "evidence_items": [{"evidence_id": "A-001", "tag": "BULL"}],
        "allowed_evidence_ids": ["A-001", "TECH-001", "TECH-002", "TECH-003"],
        "level_candidates": {"supports": [{"value": 99, "distance_pct": 1}], "display": {}},
        "market_session": {"market": "US", "timezone": "America/New_York", "phase": "postmarket"},
    }


def _decision(action: str, score: float = 63.4) -> DecisionDashboard:
    return DecisionDashboard.model_validate({
        "one_sentence": "Test decision.", "proposed_action": action, "final_action": action, "final_score": score,
        "confidence": "medium", "position_advice": PositionAdvice(no_position="Wait.", has_position="Hold."),
        "levels": TradingLevels(invalidation_condition="Support fails."), "action_checklist": ["Observe support.", "Observe volume."],
        "phase_decision": PhaseDecision(market="US", timezone="America/New_York", phase="postmarket", action_window="Next session.", immediate_action="Observe."),
        "score_explanation": "Python score is authoritative.",
    })


def test_integrity_detects_score_override_and_fake_evidence() -> None:
    context = _context()
    proposal = DecisionDashboard.model_validate({
        **_decision("watch", score=80).model_dump(),
        "catalysts": [{"text": "Fake", "evidence_ids": ["A-404"]}],
    })
    result = validate_decision_integrity(proposal, context)
    assert not result.ok
    assert "authoritative_score_override" in result.errors
    assert "invalid_catalyst_evidence_id:A-404" in result.errors


def test_guardrails_convert_no_position_add_and_sell() -> None:
    context = _context(has_position=False)
    integrity = validate_decision_integrity(_decision("add"), context)
    result = apply_guardrails(_decision("add"), context, integrity)
    assert result.final_action == "watch"
    assert [entry.to_action for entry in result.adjustments] == ["watch"]

    sell_result = apply_guardrails(_decision("sell"), context, validate_decision_integrity(_decision("sell"), context))
    assert sell_result.final_action == "avoid"


def test_finalizer_writes_fallback_and_audit_without_model(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "daily_report"
    ctx = RunContext(paths=ProjectPaths.from_root(root), ticker="AAPL", run_dir=tmp_path / "run", report_date="2026-07-27")
    ctx.run_dir.mkdir()
    ctx.data_file.write_text(json.dumps({
        "TICKER": "AAPL", "LONG_NAME": "Apple", "INSTRUMENT_TYPE": "EQUITY", "CURRENCY": "USD", "LAST_CLOSE": 100,
        "technical_score": 65, "technical_signal": "bullish", "ma20": 98, "ma50": 95, "ma200": 90,
        "bb_up": 110, "bb_dn": 92, "atr14": 2, "rsi": 55, "macd_line": 1, "signal_line": 0.5,
        "final_rating": {"final_score": 63.4, "rating_class": "hold"},
    }), encoding="utf-8")
    ctx.chart_file.write_text("<div>trusted chart</div>", encoding="utf-8")
    ctx.final_notes_json_file.write_text(json.dumps({"items": [{
        "tag": "BULL", "title": "Catalyst", "fact": "Fact", "investment_meaning": "Meaning", "evidence_id": "A-001",
    }]}), encoding="utf-8")

    def fake_render(current_ctx):
        current_ctx.final_output_html.write_text("ok", encoding="utf-8")
        return True, ""

    monkeypatch.setattr("stock_daily_agent.finalizer._render_html", fake_render)
    result = finalize_report(ctx)
    assert result.ok and result.fallback_used
    decision = json.loads(ctx.decision_file.read_text(encoding="utf-8"))
    audit = json.loads(ctx.finalization_audit_file.read_text(encoding="utf-8"))
    assert decision["final_score"] == 63.4
    assert decision["final_action"] == "watch"
    assert audit["fallback_used"] is True
    assert not ctx.decision_context_file.exists()
