"""No-network regression coverage for bounded P3 specialist opinions."""

from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "daily_report" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from stock_daily_agent.decision_guardrails import apply_opinion_guardrail
from stock_daily_agent.decision_schema import OpinionAgentOutput
from stock_daily_agent.finalizer import build_fallback_decision
from stock_daily_agent.opinion_agents import _bound_evidence_ids, summarize_opinion_conflict, synthesize_opinions


def _context() -> dict:
    return {
        "instrument": {"ticker": "APP", "current_price": 100},
        "authoritative_rating": {"final_score": 70, "rating_class": "buy"},
        "technical": {"technical_score": 70, "atr14": 2},
        "holding_context": {"has_position": False, "portfolio_weight": None},
        "data_quality": {"level": "good", "limitations": []},
        "evidence_items": [{"evidence_id": "A-001", "tag": "BULL", "fact": "Fundamental catalyst"}],
        "allowed_evidence_ids": ["A-001", "TECH-001", "TECH-002", "TECH-003"],
        "level_candidates": {"supports": [{"value": 99, "distance_pct": 1}], "display": {}},
        "market_session": {"market": "US", "timezone": "America/New_York", "phase": "postmarket"},
    }


def test_opinion_synthesis_is_tool_free_and_returns_ordered_specialists(monkeypatch) -> None:
    def fake_call(agent, _context_value, _cfg):
        return OpinionAgentOutput(agent=agent, signal="hold", confidence=0.6, reason="等待进一步确认", evidence_ids=[])

    monkeypatch.setattr("stock_daily_agent.opinion_agents._call_opinion", fake_call)
    monkeypatch.setattr("stock_daily_agent.opinion_agents.build_llm_cfg", lambda *_args: {})
    result = synthesize_opinions(_context(), provider="dashscope", model="qwen-plus")
    assert [item.agent for item in result.opinions] == ["technical", "news_fundamental", "risk"]
    assert not result.errors
    source = (AGENT_SRC / "stock_daily_agent" / "opinion_agents.py").read_text(encoding="utf-8")
    assert "functions=" not in source
    assert "web_search" not in source


def test_opinion_evidence_ids_are_safely_bounded_before_schema_validation() -> None:
    payload = {
        "agent": "news_fundamental",
        "signal": "hold",
        "confidence": 0.6,
        "reason": "证据数量较多。",
        "evidence_ids": ["A-001", "A-002", "A-003", "A-004", "A-005", "A-006"],
    }
    bounded = _bound_evidence_ids(payload)
    opinion = OpinionAgentOutput.model_validate(bounded)

    assert opinion.evidence_ids == ["A-001", "A-002", "A-003", "A-004", "A-005"]
    assert payload["evidence_ids"][-1] == "A-006"  # no mutation of raw model payload


def test_risk_or_conflict_can_only_downgrade_optimistic_action() -> None:
    context = _context()
    decision = build_fallback_decision(context, "test")
    # The fallback begins at WATCH for an unheld buy rating; make an optimistic
    # decision to exercise P3's one-way safety guardrail.
    payload = decision.model_dump()
    payload.update({"proposed_action": "buy", "final_action": "buy", "adjustments": []})
    decision = type(decision).model_validate(payload)
    opinions = [
        OpinionAgentOutput(agent="technical", signal="buy", confidence=0.8, reason="趋势改善", evidence_ids=["TECH-001"]),
        OpinionAgentOutput(agent="news_fundamental", signal="buy", confidence=0.7, reason="基本面支持", evidence_ids=["A-001"]),
        OpinionAgentOutput(agent="risk", signal="sell", confidence=0.8, reason="风险偏高", evidence_ids=[]),
    ]
    guarded = apply_opinion_guardrail(decision, context, opinions)
    assert guarded.final_action == "watch"
    assert guarded.final_score == 70
    assert "风险意见" in guarded.adjustments[-1].reason
    assert "分歧" in (summarize_opinion_conflict(opinions) or "")
