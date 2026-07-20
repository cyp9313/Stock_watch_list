from __future__ import annotations

import math

import numpy as np
import pandas as pd

from daily_report.src.stock_daily_agent.evidence_summarizer import _apply_summaries_by_uid
from daily_report.src.stock_daily_agent.research_gap_analyzer import _compute_ticker_gaps
from portfolio_analysis.action_targets import apply_deterministic_action_targets
from portfolio_analysis.metric_contracts import (
    evidence_coverage_score,
    evidence_freshness_score,
    evidence_verification_score,
)
from portfolio_analysis.metrics import calculate_portfolio_metrics
from portfolio_analysis.observation_view import _build_observation_view
from portfolio_analysis.report_quality import evaluate_report_quality
from portfolio_analysis.return_model import (
    build_portfolio_return_model,
    risk_contributions,
    scenario_volatility,
)
from portfolio_analysis.validators import validate_portfolio_claims


def _plan(event_need: str = "earnings_date") -> dict:
    return {
        "tickers": [
            {
                "ticker": "TSLA",
                "research_questions": [
                    {"question_id": "q1", "event_need": event_need},
                ],
            }
        ]
    }


def _meta() -> dict:
    return {
        "TSLA": {
            "name": "Tesla Inc",
            "instrument_type": "EQUITY",
            "official_domains": ["tesla.com"],
        }
    }


def test_quote_page_does_not_satisfy_first_pass_gap_need():
    raw = [
        {
            "ticker": "TSLA",
            "event_hint": "earnings_results",
            "title": "Tesla Stock Quote and Price",
            "summary": "Live TSLA chart and historical price performance.",
            "url": "https://finance.yahoo.com/quote/TSLA",
        }
    ]
    gaps = _compute_ticker_gaps(
        _plan("earnings_results"), raw, first_pass=True, instrument_metadata=_meta()
    )
    assert gaps[0]["found_needs"] == []
    assert gaps[0]["missing_needs"] == ["earnings_results"]


def test_official_event_satisfies_first_pass_gap_need():
    raw = [
        {
            "ticker": "TSLA",
            "event_hint": "earnings_date",
            "title": "Tesla Announces Date for Second Quarter 2026 Financial Results",
            "summary": "Tesla will publish its earnings results and hold a conference call.",
            "url": "https://ir.tesla.com/press-release/financial-results-date",
        }
    ]
    gaps = _compute_ticker_gaps(
        _plan("earnings_date"), raw, first_pass=True, instrument_metadata=_meta()
    )
    assert gaps[0]["found_needs"] == ["earnings_date"]
    assert gaps[0]["missing_needs"] == []


def test_post_materiality_gap_requires_materiality_acceptance():
    evidence = [
        {
            "ticker": "TSLA",
            "event_hint": "earnings_date",
            "title": "Tesla earnings date",
            "url": "https://ir.tesla.com/test",
            "materiality_accepted": False,
            "accept": True,
        }
    ]
    gaps = _compute_ticker_gaps(
        _plan("earnings_date"), evidence, first_pass=False, instrument_metadata=_meta()
    )
    assert gaps[0]["missing_needs"] == ["earnings_date"]


def test_summarizer_missing_accept_is_rejected():
    evidence = [{"evidence_uid": "ev_1", "ticker": "TSLA", "title": "Event"}]
    errors = _apply_summaries_by_uid(
        evidence,
        [{"evidence_uid": "ev_1", "ticker": "TSLA", "event_title_zh": "事件"}],
    )
    assert evidence[0]["accept"] is False
    assert any("missing_accept" in error for error in errors)


def test_summarizer_uidless_item_does_not_shift_mapping():
    evidence = [
        {"evidence_uid": "ev_a", "ticker": "TSLA", "title": "A"},
        {"evidence_uid": "ev_b", "ticker": "TSLA", "title": "B"},
    ]
    errors = _apply_summaries_by_uid(
        evidence,
        [
            {"evidence_uid": "ev_a", "ticker": "TSLA", "accept": True, "event_title_zh": "A摘要"},
            {"ticker": "TSLA", "accept": True, "event_title_zh": "无UID"},
            {"evidence_uid": "ev_b", "ticker": "TSLA", "accept": False, "event_title_zh": "B摘要"},
        ],
    )
    assert evidence[0]["summary_zh"] == "A摘要"
    assert evidence[1]["summary_zh"] == "B摘要"
    assert evidence[1]["accept"] is False
    assert any("item_missing_uid" in error for error in errors)


def _price_frame(days: int = 90) -> pd.DataFrame:
    index = pd.bdate_range("2026-01-02", periods=days)
    rng = np.random.default_rng(42)
    a = 100 * np.cumprod(1 + rng.normal(0.0005, 0.012, days))
    b = 80 * np.cumprod(1 + rng.normal(0.0002, 0.010, days))
    bench = 200 * np.cumprod(1 + rng.normal(0.0003, 0.008, days))
    return pd.DataFrame({"AAA": a, "BBB": b, "^GSPC": bench}, index=index)


def test_empty_return_model_is_constructible():
    model = build_portfolio_return_model(pd.DataFrame(), {})
    assert model.daily_returns.empty
    assert model.benchmark_cumulative_returns.empty
    assert model.beta_observations == 0


def test_scenario_uses_same_annualized_covariance_as_overview():
    model = build_portfolio_return_model(
        _price_frame(), {"AAA": 0.6, "BBB": 0.4}, benchmark="^GSPC"
    )
    result = scenario_volatility(
        model,
        {"AAA": 0.6, "BBB": 0.4},
        {"AAA": 0.5, "BBB": 0.4},
    )
    assert result["overview_volatility_check"] is True
    assert math.isclose(result["current_volatility"], model.annualized_volatility, abs_tol=1e-4)


def test_positive_risk_contributions_are_normalized():
    model = build_portfolio_return_model(
        _price_frame(), {"AAA": 0.6, "BBB": 0.4}, benchmark="^GSPC"
    )
    rows = risk_contributions(model, {"AAA": 0.6, "BBB": 0.4})
    assert rows
    assert all(row["risk_contribution"] >= 0 for row in rows)
    assert math.isclose(sum(row["risk_contribution"] for row in rows), 1.0, abs_tol=1e-9)


def test_metrics_beta_status_and_observation_count_are_actual():
    close = _price_frame()
    snapshot = {
        "benchmark": "^GSPC",
        "holdings": [
            {"ticker": "AAA", "weight": 0.6, "return_1d": 0, "return_ytd": 0},
            {"ticker": "BBB", "weight": 0.4, "return_1d": 0, "return_ytd": 0},
        ],
        "instrument_metadata": {},
    }
    model = build_portfolio_return_model(close, {"AAA": 0.6, "BBB": 0.4})
    metrics = calculate_portfolio_metrics(snapshot, close, return_model=model)
    assert metrics["portfolio_beta_status"] == "actual"
    assert metrics["portfolio_beta_source"] == "return_model"
    assert metrics["portfolio_beta_observations"] == model.beta_observations


def test_no_evidence_metrics_are_zero_not_display_floors():
    assert evidence_coverage_score([], ["TSLA"], floor=0.0) == 0.0
    assert evidence_freshness_score([], empty_score=0.0, floor=0.0) == 0.0
    assert evidence_verification_score([], empty_score=0.0, floor=0.0) == 0.0


def test_action_targets_use_return_model_scenario():
    model = build_portfolio_return_model(
        _price_frame(), {"AAA": 0.6, "BBB": 0.4}, benchmark="^GSPC"
    )
    metrics = {
        "risk_contributions": risk_contributions(model, {"AAA": 0.6, "BBB": 0.4})
    }
    advice = {
        "actions": [
            {"ticker": "AAA", "action": "reduce", "current_weight": 0.6},
        ]
    }
    result = apply_deterministic_action_targets(
        advice, metrics, {}, return_model=model
    )
    change = result["actions"][0]["expected_risk_change"]
    assert change["method"] == "return_model_annualized_covariance"
    assert change["overview_volatility_check"] is True


def test_report_quality_degrades_when_bad_summary_items_are_quarantined():
    quality = evaluate_report_quality(
        {},
        {"portfolio_beta_status": "actual"},
        {
            "status": "insufficient_coverage",
            "evidence": [],
            "accepted_evidence": [],
            "diagnostics": {
                "status": "insufficient_coverage",
                "summarizer_errors": ["summarizer_missing_uids:ev_1"],
                "accepted_top_risk_coverage": 0.0,
                "accepted_risk_weighted_coverage": 0.0,
            },
        },
        {"confidence": 0.0, "final_confidence": 0.0, "actions": []},
        {},
    )
    assert quality["publishable"] is True
    assert quality["observation_only"] is True
    assert "summarizer_integrity_error_detected" not in quality["blocking_errors"]
    assert any("Fail-closed" in warning for warning in quality["warnings"])


def test_report_quality_blocks_unsafe_accepted_after_summarizer_error():
    unsafe = {
        "evidence_uid": "ev_1",
        "evidence_id": "E001",
        "ticker": "TSLA",
        # Missing summary_integrity_ok proves this item was not promoted by the
        # per-item integrity gate.
    }
    quality = evaluate_report_quality(
        {},
        {"portfolio_beta_status": "actual"},
        {
            "status": "success",
            "evidence": [unsafe],
            "accepted_evidence": [unsafe],
            "diagnostics": {
                "status": "success",
                "summarizer_errors": ["summarizer_missing_uids:ev_2"],
                "accepted_top_risk_coverage": 1.0,
                "accepted_risk_weighted_coverage": 1.0,
            },
        },
        {"confidence": 0.8, "final_confidence": 0.8, "actions": []},
        {},
    )
    assert quality["publishable"] is False
    assert "summarizer_integrity_error_detected" in quality["blocking_errors"]


def test_report_quality_keeps_safe_accepted_subset_after_partial_summary_error():
    safe = {
        "evidence_uid": "ev_1",
        "evidence_id": "E001",
        "ticker": "TSLA",
        "summary_integrity_ok": True,
        "recency_tier": "fresh_event",
        "article_fetch_ok": True,
    }
    quality = evaluate_report_quality(
        {},
        {"portfolio_beta_status": "actual"},
        {
            "status": "success",
            "evidence": [safe],
            "accepted_evidence": [safe],
            "diagnostics": {
                "status": "success",
                "summarizer_errors": ["summarizer_missing_uids:ev_2"],
                "accepted_top_risk_coverage": 1.0,
                "accepted_risk_weighted_coverage": 1.0,
            },
        },
        {"confidence": 0.8, "final_confidence": 0.8, "actions": []},
        {},
    )
    assert quality["publishable"] is True
    assert "summarizer_integrity_error_detected" not in quality["blocking_errors"]


def test_observation_view_uses_deterministic_top5_members():
    advice = {
        "executive_summary": ["错误 Top5: AAA/BBB/CCC"],
        "portfolio_analysis": {},
        "key_risks": [],
        "watch_items": [],
        "data_limitations": [],
    }
    snapshot = {
        "holdings": [
            {"ticker": "A", "price_vs_ema50_pct": -1},
            {"ticker": "B", "price_vs_ema50_pct": -2},
            {"ticker": "C", "price_vs_ema50_pct": 1},
        ]
    }
    metrics = {
        "portfolio_risk_score": 55,
        "portfolio_risk_level": "medium_high",
        "relative_returns": {},
        "aggregates": {
            "below_ema50_weight": 0.7,
            "top_risk_contribution_sum": 0.5,
            "top_risk_weight_sum": 0.4,
            "top5_below_ema50_count": 2,
            "top5_count": 3,
            "top5_risk_contributors": [
                {"ticker": "A"}, {"ticker": "B"}, {"ticker": "C"},
            ],
        },
    }
    result = _build_observation_view(advice, snapshot, metrics, {"top_risk_tickers": ["A", "B", "C"]}, [])
    text = "\n".join(result["executive_summary"])
    assert "A、B、C" in text
    assert "AAA/BBB/CCC" not in text
    assert result["observation_only"] is True


def test_claim_validator_rejects_wrong_top5_and_holdings_overlap_claim():
    snapshot = {
        "holdings": [
            {"ticker": ticker, "rsi_regime": "neutral"}
            for ticker in ["A", "B", "C", "D", "E", "F"]
        ]
    }
    metrics = {
        "relative_returns": {},
        "aggregates": {
            "top_risk_contribution_sum": 0.5,
            "top5_risk_contributors": [
                {"ticker": ticker} for ticker in ["A", "B", "C", "D", "E"]
            ],
        },
    }
    advice = {
        "executive_summary": [
            "Top5 风险贡献者为 A、B、C、D、F，底层持仓存在重复宽基暴露。"
        ],
        "portfolio_analysis": {},
        "key_risks": [],
        "actions": [],
        "watch_items": [],
    }
    errors, _ = validate_portfolio_claims(advice, snapshot, metrics, [])
    assert any("Top5 成员" in error for error in errors)
    assert any("ETF holdings" in error for error in errors)


def test_short_history_ticker_does_not_poison_covariance_model():
    close = _price_frame(120)
    close["SHORT"] = np.nan
    close.loc[close.index[-18:], "SHORT"] = np.linspace(10.0, 12.0, 18)
    model = build_portfolio_return_model(
        close,
        {"AAA": 0.60, "BBB": 0.395, "SHORT": 0.005},
        benchmark="^GSPC",
    )
    assert model.covariance_matrix is not None
    assert "SHORT" in model.covariance_excluded_tickers
    assert model.covariance_weight_coverage >= 0.99
    full_weights = {"AAA": 0.60, "BBB": 0.395, "SHORT": 0.005}
    rows = risk_contributions(model, full_weights)
    assert rows
    assert {row["ticker"] for row in rows} == {"AAA", "BBB"}
    scenario = scenario_volatility(model, full_weights, full_weights)
    assert scenario["overview_volatility_check"] is True


def test_observation_mode_rewrites_existing_hold_narrative():
    from daily_report.run_portfolio_report import _enforce_observation_mode

    advice = {
        "actions": [
            {
                "ticker": "AAA",
                "action": "hold",
                "current_weight": 0.2,
                "portfolio_reason": "底层持仓重复并且流动性压力驱动风险。",
                "news_reason": "基本面恶化，投资者恐慌。",
                "execute_if": ["立即执行"],
            }
        ]
    }
    result = _enforce_observation_mode(advice)
    action = result["actions"][0]
    assert action["action"] == "watch"
    assert action["target_weight_min"] == 0.2
    assert action["target_weight_max"] == 0.2
    visible = " ".join(
        str(action.get(key) or "")
        for key in ("portfolio_reason", "technical_reason", "news_reason", "bull_case", "bear_case")
    )
    assert "底层持仓重复" not in visible
    assert "流动性压力" not in visible
    assert "基本面恶化" not in visible
    assert "投资者恐慌" not in visible
    assert action["execute_if"] == []
