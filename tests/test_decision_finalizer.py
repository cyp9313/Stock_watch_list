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
from stock_daily_agent.finalizer import _repair_risk_boundary_conflicts, finalize_report
from stock_daily_agent.opinion_agents import OpinionSynthesisResult
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


def test_finalizer_respects_dashboard_disable_switch(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setenv("DECISION_REPORT_ENABLED", "false")
    result = finalize_report(ctx)
    assert result.ok and not result.fallback_used
    audit = json.loads(ctx.finalization_audit_file.read_text(encoding="utf-8"))
    assert not ctx.decision_file.exists()
    assert audit["decision_enabled"] is False
    assert audit["fallback_used"] is False
    assert not ctx.decision_context_file.exists()


def test_finalizer_attaches_bounded_opinions_and_applies_only_safety_downgrade(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setenv("DECISION_REPORT_ENABLED", "true")
    monkeypatch.setenv("DECISION_OPINION_AGENTS_ENABLED", "true")
    from stock_daily_agent.decision_schema import OpinionAgentOutput
    monkeypatch.setattr("stock_daily_agent.opinion_agents.synthesize_opinions", lambda *_args, **_kwargs: OpinionSynthesisResult(
        opinions=[
            OpinionAgentOutput(agent="technical", signal="buy", confidence=0.8, reason="趋势改善"),
            OpinionAgentOutput(agent="news_fundamental", signal="buy", confidence=0.8, reason="基本面支持", evidence_ids=["A-001"]),
            OpinionAgentOutput(agent="risk", signal="sell", confidence=0.8, reason="风险偏高"),
        ], errors={}, provider="dashscope", model="qwen-plus", elapsed_seconds=0.01,
    ))
    result = finalize_report(ctx, proposal=_decision("buy"))
    assert result.ok
    decision = json.loads(ctx.decision_file.read_text(encoding="utf-8"))
    audit = json.loads(ctx.finalization_audit_file.read_text(encoding="utf-8"))
    assert decision["final_score"] == 63.4
    assert decision["final_action"] == "watch"
    assert len(decision["agent_opinions"]) == 3
    assert audit["opinion_agents"]["completed"] == 3


def test_finalizer_audit_keeps_initial_integrity_failure_after_fallback(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "daily_report"
    ctx = RunContext(paths=ProjectPaths.from_root(root), ticker="AAPL", run_dir=tmp_path / "run", report_date="2026-07-27")
    ctx.run_dir.mkdir()
    ctx.data_file.write_text(json.dumps({
        "TICKER": "AAPL", "LONG_NAME": "Apple", "INSTRUMENT_TYPE": "EQUITY", "CURRENCY": "USD", "LAST_CLOSE": 100,
        "technical_score": 65, "technical_signal": "bullish", "ma20": 98, "ma50": 95, "ma200": 90,
        "bb_up": 110, "bb_dn": 92, "atr14": 2, "rsi": 55, "macd_line": 1, "signal_line": 0.5,
        "final_rating": {"final_score": 43.2, "rating_class": "hold", "rating_text": "观察等待 WATCH"},
    }), encoding="utf-8")
    ctx.chart_file.write_text("<div>trusted chart</div>", encoding="utf-8")
    ctx.final_notes_json_file.write_text(json.dumps({"items": [{
        "tag": "BULL", "title": "Catalyst", "fact": "Fact", "investment_meaning": "Meaning", "evidence_id": "A-001",
    }]}), encoding="utf-8")

    def fake_render(current_ctx):
        current_ctx.final_output_html.write_text("ok", encoding="utf-8")
        return True, ""

    monkeypatch.setattr("stock_daily_agent.finalizer._render_html", fake_render)
    bad_payload = _decision("watch", score=43.2).model_dump()
    bad_payload["catalysts"] = [{"text": "Bad evidence", "evidence_ids": ["A-404"]}]
    result = finalize_report(ctx, proposal=DecisionDashboard.model_validate(bad_payload))

    audit = json.loads(ctx.finalization_audit_file.read_text(encoding="utf-8"))
    decision = json.loads(ctx.decision_file.read_text(encoding="utf-8"))
    assert result.ok and result.fallback_used
    assert "invalid_catalyst_evidence_id:A-404" in audit["proposal_integrity"]["errors"]
    assert "invalid_catalyst_evidence_id:A-404" not in audit["integrity"]["errors"]
    assert audit["proposal_score"] == 43.2
    assert decision["authoritative_rating_text"]


def test_risk_boundary_conflict_is_repaired_without_discarding_valid_decision() -> None:
    context = {
        **_context(has_position=True),
        "level_candidates": {
            "supports": [{"value": 99, "distance_pct": 1}],
            "display": {"stop_loss": "$90–$95"},
        },
    }
    decision = DecisionDashboard.model_validate({
        "one_sentence": "\u7b49\u5f85\u8d8b\u52bf\u786e\u8ba4\u3002",
        "proposed_action": "hold", "final_action": "hold", "final_score": 63.4, "confidence": "medium",
        "position_advice": {
            "no_position": "\u6682\u4e0d\u5f00\u4ed3\u3002",
            "has_position": "\u82e5\u8dcc\u7834 $80\uff0c\u5219\u51cf\u4ed3\u3002",
        },
        "levels": {"invalidation_condition": "\u82e5\u8dcc\u7834\u6b62\u635f\u53c2\u8003\u533a\u95f4\u5219\u91cd\u65b0\u8bc4\u4f30\u3002"},
        "action_checklist": ["\u5173\u6ce8\u8d8b\u52bf\u786e\u8ba4\u3002", "\u9075\u5b88\u98ce\u9669\u63a7\u5236\u3002"],
        "phase_decision": {
            "market": "US", "timezone": "America/New_York", "phase": "postmarket",
            "action_window": "\u4e0b\u4e00\u4e2a\u4ea4\u6613\u65e5\u3002", "immediate_action": "\u7b49\u5f85\u786e\u8ba4\u3002",
        },
        "score_explanation": "\u7a0b\u5e8f\u8bc4\u5206\u4fdd\u6301\u6743\u5a01\u3002",
    })
    integrity = validate_decision_integrity(decision, context)
    repaired, fields = _repair_risk_boundary_conflicts(decision, context, integrity)

    assert fields == ["position_advice.has_position"]
    assert repaired.risk_boundary_guardrail_applied is True
    assert repaired.fallback_used is False
    assert validate_decision_integrity(repaired, context).ok
