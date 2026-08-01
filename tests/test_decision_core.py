"""Unit coverage for the deterministic decision-dashboard foundation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "daily_report" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from stock_daily_agent.config import ProjectPaths, RunContext
from stock_daily_agent.decision_context import build_decision_context, normalize_holding_context
from stock_daily_agent.decision_levels import calculate_level_plan
from stock_daily_agent.decision_schema import (
    DecisionAdjustment,
    DecisionDashboard,
    PhaseDecision,
    PositionAdvice,
    TradingLevels,
)
from stock_daily_agent.market_session import infer_market_session


def _dashboard(**overrides):
    payload = {
        "one_sentence": "Wait for confirmation near support.",
        "proposed_action": "watch",
        "final_action": "watch",
        "final_score": 63.4,
        "confidence": "medium",
        "position_advice": PositionAdvice(no_position="Wait.", has_position="Hold risk steady."),
        "levels": TradingLevels(invalidation_condition="A decisive break below support invalidates the setup."),
        "phase_decision": PhaseDecision(
            market="US", timezone="America/New_York", phase="postmarket",
            action_window="Review on the next regular session.", immediate_action="Observe.",
        ),
        "score_explanation": "The deterministic rating remains authoritative.",
    }
    payload.update(overrides)
    return DecisionDashboard(**payload)


def test_schema_rejects_extra_fields_and_broken_adjustment_chain() -> None:
    with pytest.raises(ValidationError):
        _dashboard(unexpected="not allowed")
    with pytest.raises(ValidationError):
        _dashboard(
            final_action="hold",
            adjustments=[DecisionAdjustment(from_action="buy", to_action="hold", reason="test")],
        )


def test_schema_accepts_continuous_adjustment_chain() -> None:
    decision = _dashboard(
        proposed_action="buy",
        final_action="watch",
        adjustments=[
            DecisionAdjustment(from_action="buy", to_action="hold", reason="limited data"),
            DecisionAdjustment(from_action="hold", to_action="watch", reason="no confirmation"),
        ],
    )
    assert decision.final_action == "watch"


@pytest.mark.parametrize(
    ("ticker", "instrument_type", "when", "market", "phase"),
    [
        ("AAPL", "EQUITY", datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc), "US", "open"),
        ("0700.HK", "EQUITY", datetime(2026, 7, 27, 4, 30, tzinfo=timezone.utc), "HK", "lunch_break"),
        ("BTC-USD", "CRYPTO", datetime(2026, 7, 25, 4, 30, tzinfo=timezone.utc), "CRYPTO", "continuous"),
        ("SAP.DE", "EQUITY", datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc), "DE", "non_trading"),
    ],
)
def test_market_sessions_are_timezone_deterministic(ticker, instrument_type, when, market, phase) -> None:
    result = infer_market_session(ticker, instrument_type=instrument_type, now=when)
    assert result["market"] == market
    assert result["phase"] == phase


def test_level_plan_filters_wrong_side_and_merges_nearby_levels() -> None:
    plan = calculate_level_plan({
        "LAST_CLOSE": 100,
        "ma20": 99.8,
        "ma50": 99.9,
        "ma200": 92,
        "bb_dn": 98,
        "bb_up": 111,
        "FIFTY2W_LO": 70,
        "FIFTY2W_HI": 130,
        "RECENT_LOW_20D": 98.1,
        "RECENT_HIGH_20D": 108,
        "atr14": 2,
        "chip_profile_primary": {"poc_price": 99.85, "value_area_low": 98.2, "value_area_high": 109},
    })
    assert all(item.value <= 100 for item in plan.supports)
    assert all(item.value >= 100 for item in plan.resistances)
    assert plan.ideal_buy is not None and plan.stop_loss is not None
    assert plan.stop_loss < plan.ideal_buy <= 100


def test_holding_context_whitelists_and_context_stays_compact(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        normalize_holding_context({"has_position": False, "email": "private@example.com"})
    root = tmp_path / "daily_report"
    paths = ProjectPaths.from_root(root)
    ctx = RunContext(paths=paths, ticker="AAPL", run_dir=tmp_path / "run")
    ctx.run_dir.mkdir()
    ctx.data_file.write_text(json.dumps({
        "TICKER": "AAPL", "LONG_NAME": "Apple Inc.", "INSTRUMENT_TYPE": "EQUITY", "CURRENCY": "USD",
        "LAST_CLOSE": 100, "data_end": "2026-07-27", "technical_score": 65, "technical_signal": "bullish",
        "ma20": 98, "ma50": 95, "ma200": 90, "bb_up": 110, "bb_dn": 92, "atr14": 2,
        "rsi": 55, "macd_line": 1, "signal_line": 0.5, "vol_ratio": 1.2,
        "final_rating": {"final_score": 63.4, "rating_class": "hold", "subscores": {"technical_score": 65}},
    }), encoding="utf-8")
    ctx.final_notes_json_file.write_text(json.dumps({"items": [{
        "tag": "BULL", "title": "x" * 300, "fact": "f" * 900, "logic": "l", "investment_meaning": "i",
        "source": "source", "source_date": "2026-07-27", "evidence_id": "A-001",
    }]}), encoding="utf-8")
    context = build_decision_context(ctx, holding_context={"has_position": True, "shares": 2})
    assert context["authoritative_rating"]["final_score"] == 63.4
    assert context["holding_context"] == {"has_position": True, "buy_price": None, "shares": 2.0, "portfolio_weight": None, "unrealized_pnl_pct": None}
    assert context["evidence_items"][0]["title"] == "x" * 160
    assert "private@example.com" not in json.dumps(context)
