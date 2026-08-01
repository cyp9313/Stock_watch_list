"""Service and CLI compatibility checks for decision-dashboard inputs."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "daily_report" / "src"))

from daily_report.service import _normalize_holding_context
from stock_daily_agent.cli import build_parser


def test_service_holding_context_is_minimal_and_finite() -> None:
    assert _normalize_holding_context({"has_position": True, "buy_price": 10, "shares": 2}) == {
        "has_position": True, "buy_price": 10.0, "shares": 2.0, "portfolio_weight": None, "unrealized_pnl_pct": None,
    }
    with pytest.raises(ValueError):
        _normalize_holding_context({"has_position": True, "email": "private@example.com"})
    with pytest.raises(ValueError):
        _normalize_holding_context({"has_position": True, "portfolio_weight": 1.1})


def test_cli_exposes_local_holding_and_decision_overrides() -> None:
    args = build_parser().parse_args([
        "AAPL", "--holding-context-json", "run/holding.json", "--disable-decision-dashboard",
        "--decision-provider", "dashscope", "--decision-model", "qwen-plus",
    ])
    assert args.holding_context_json == "run/holding.json"
    assert args.disable_decision_dashboard
    assert args.decision_provider == "dashscope"
