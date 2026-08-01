"""No-network tests for controlled one-call decision synthesis."""

from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "daily_report" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from stock_daily_agent.decision_synthesizer import synthesize_decision


def _context() -> dict:
    return {
        "authoritative_rating": {"final_score": 63.4}, "allowed_evidence_ids": ["A-001", "TECH-001"],
        "level_candidates": {"display": {"ideal_buy": "$98.00–$99.00", "secondary_buy": None, "stop_loss": "$96.00–$97.00", "take_profit": "$108.00–$109.00"}},
        "market_session": {"market": "US", "timezone": "America/New_York", "phase": "postmarket"},
    }


def _valid_json() -> str:
    return '''{
      "one_sentence":"Wait for confirmation.","proposed_action":"watch","final_action":"watch","final_score":63.4,"confidence":"medium",
      "position_advice":{"no_position":"Wait.","has_position":"Hold."},
      "levels":{"ideal_buy":"$98.00–$99.00","secondary_buy":null,"stop_loss":"$96.00–$97.00","take_profit":"$108.00–$109.00","invalidation_condition":"Support fails."},
      "catalysts":[{"text":"Catalyst","evidence_ids":["A-001"]}],"risk_alerts":[],"action_checklist":["Observe support.","Observe volume."],
      "phase_decision":{"market":"US","timezone":"America/New_York","phase":"postmarket","action_window":"Next session.","immediate_action":"Observe."},
      "score_explanation":"Python remains authoritative.","adjustments":[],"fallback_used":false,"fallback_reason":null
    }'''


def test_synthesis_validates_json_from_mocked_tool_free_call(monkeypatch) -> None:
    monkeypatch.setattr("stock_daily_agent.decision_synthesizer._call_model", lambda messages, cfg: "```json\n" + _valid_json() + "\n```")
    monkeypatch.setenv("DECISION_REPORT_PROVIDER", "dashscope")
    monkeypatch.setenv("DECISION_REPORT_MODEL", "qwen-plus")
    result = synthesize_decision(_context())
    assert result.ok
    assert result.proposal is not None
    assert result.proposal.final_score == 63.4


def test_synthesis_rejects_invalid_json_without_retry(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("stock_daily_agent.decision_synthesizer._call_model", lambda messages, cfg: calls.append(1) or "not json")
    result = synthesize_decision(_context(), provider="dashscope", model="qwen-plus")
    assert not result.ok
    assert len(calls) == 1
