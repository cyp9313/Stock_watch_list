from __future__ import annotations

from daily_report.scripts.build_portfolio_report import build_html
from daily_report.src.stock_daily_agent.research_core.entity_resolution import resolve_primary_entity
from daily_report.src.stock_daily_agent.research_query_compiler import expand_official_lane_queries
from portfolio_analysis.instrument_metadata import build_instrument_metadata
from portfolio_analysis.observation_view import _build_observation_view
from portfolio_analysis.report_quality import evaluate_report_quality


def _snapshot() -> dict:
    return {
        "portfolio_name": "Portfolio",
        "report_date": "2026-07-19",
        "as_of": "2026-07-19T20:25:00+02:00",
        "base_currency": "EUR",
        "benchmark": "^GSPC",
        "summary": {"total_market_value_base": 1000.0},
        "data_quality": {},
        "holdings": [
            {
                "ticker": "SOFI",
                "weight": 1.0,
                "market_value_base": 1000.0,
                "price_vs_ema50_pct": -1.0,
            }
        ],
    }


def test_observation_view_uses_python_risk_level_and_chinese_summary():
    advice = {
        "report_mode": "quantitative_fallback",
        "risk_level": "medium",
        "confidence": 0.4,
        "actions": [],
        "data_limitations": [],
    }
    metrics = {
        "portfolio_risk_score": 54,
        "portfolio_risk_level": "medium_high",
        "relative_returns": {},
        "aggregates": {
            "below_ema50_weight": 1.0,
            "top_risk_contribution_sum": 1.0,
            "top_risk_weight_sum": 1.0,
            "top5_below_ema50_count": 1,
            "top5_count": 1,
            "top5_risk_contributors": [{"ticker": "SOFI"}],
        },
    }
    result = _build_observation_view(
        advice, _snapshot(), metrics, {"top_risk_tickers": ["SOFI"]}, [],
    )
    assert result["risk_level"] == "medium_high"
    assert any("风险等级为中高" in item for item in result["executive_summary"])
    assert all("medium_high" not in item for item in result["executive_summary"])


def test_observation_html_preserves_zero_confidence_and_uses_non_ai_titles():
    snapshot = _snapshot()
    metrics = {
        "portfolio_risk_score": 54,
        "portfolio_risk_level": "medium_high",
        "risk_score_confidence": 0.85,
        "risk_score_component_max": {},
        "relative_returns": {},
        "risk_contributions": [{"ticker": "SOFI", "risk_contribution": 1.0}],
        "aggregates": {
            "below_ema50_weight": 1.0,
            "top_risk_contribution_sum": 1.0,
            "top_risk_weight_sum": 1.0,
            "top5_below_ema50_count": 1,
            "top5_count": 1,
            "top5_risk_contributors": [{"ticker": "SOFI"}],
        },
    }
    advice = {
        "report_mode": "quantitative_fallback",
        "observation_only": True,
        "portfolio_stance": "observe",
        "risk_level": "medium_high",
        "confidence": 0.4,
        "final_confidence": 0.0,
        "confidence_components": {
            "model_confidence": 0.4,
            "data_quality": 1.0,
            "metadata_coverage": 0.8,
            "evidence_coverage": 0.0,
            "evidence_freshness": 0.0,
            "evidence_verification": 0.0,
        },
        "executive_summary": ["确定性观察。"],
        "key_risks": [{"risk_id": "Q1", "title": "风险贡献集中", "severity": "high"}],
        "actions": [
            {
                "ticker": "SOFI",
                "action": "watch",
                "priority": 1,
                "current_weight": 1.0,
                "target_weight_min": 1.0,
                "target_weight_max": 1.0,
                "confidence": 0.0,
            }
        ],
        "portfolio_reallocation": {
            "estimated_weight_reduction": 0.0,
            "note": "无再平衡。",
        },
        "portfolio_analysis": {},
        "watch_items": [],
        "data_limitations": [],
    }
    diagnostics = {
        "status": "insufficient_coverage",
        "raw_results_count": 107,
        "filtered_results_count": 20,
        "selected_evidence_count": 15,
        "accepted_evidence_count": 0,
        "rejected_evidence_count": 16,
        "accepted_top_risk_coverage": 0.0,
        "accepted_risk_weighted_coverage": 0.0,
        "materiality_stats": {
            "accepted_count": 0,
            "rejected_count": 15,
            "cluster_count": 15,
            "rejected_reasons": {"primary_entity_score_below_0.7": 13},
        },
    }
    quality = {
        "publishable": True,
        "observation_only": True,
        "actionable": False,
        "quality_score": 0.0,
        "research_sufficiency": 0.0,
        "final_confidence": 0.0,
    }
    html = build_html(
        snapshot,
        metrics,
        {"items": [{"ticker": "SOFI"}], "top_risk_tickers": ["SOFI"]},
        advice,
        [],
        settings={"risk_profile": "growth", "investment_horizon": "12m+"},
        fallback_reason="本轮研究未产生 Accepted Evidence。",
        research_diagnostics=diagnostics,
        report_quality=quality,
        rejected_evidence=[{} for _ in range(16)],
    )
    assert "量化投资组合观察报告" in html
    assert "报告决策置信度" in html
    assert "报告决策置信度</div><div class=\"kpi-value \" >40.00%" not in html
    assert "报告最终置信度：0.00%" in html
    assert "风险等级 中高" in html
    assert "组合态度 观察" in html
    assert "风险偏好 成长" in html
    assert "投资期限 12个月以上" in html
    assert "进入 Materiality：15" in html
    assert "摘要/身份隔离：1" in html
    assert "重点观察清单" in html
    assert "重点观察详情" in html
    assert "AI 操作建议总表" not in html
    assert "AI 操作建议详情" not in html
    assert "Portfolio 再平衡摘要" not in html


def test_report_quality_does_not_replace_explicit_zero_final_confidence():
    result = evaluate_report_quality(
        _snapshot(),
        {"portfolio_beta_status": "actual"},
        {
            "status": "insufficient_coverage",
            "evidence": [],
            "accepted_evidence": [],
            "diagnostics": {
                "status": "insufficient_coverage",
                "accepted_top_risk_coverage": 0.0,
                "accepted_risk_weighted_coverage": 0.0,
            },
        },
        {"confidence": 0.4, "final_confidence": 0.0, "actions": []},
        {},
    )
    assert result["decision_confidence"] == 0.0
    assert result["quality_score"] == 0.0


def test_official_lane_query_retains_compiler_owned_site_operator():
    compiled = [
        {
            "raw_query": "SoFi Technologies Q2 2026 earnings date results guidance",
            "language": "en",
            "lookback_days": 30,
            "lane": "official_and_news",
            "ticker": "SOFI",
            "event_need": "earnings_date",
            "question_id": "SOFI_Q1",
            "preferred_domains": ["https://investors.sofi.com/news/default.aspx"],
            "required_entities": ["SoFi Technologies"],
            "exclude_terms": [],
        }
    ]
    expanded = expand_official_lane_queries(compiled)
    assert len(expanded) == 1
    assert expanded[0]["query"].startswith("site:investors.sofi.com ")
    assert expanded[0]["preferred_domains"] == ["investors.sofi.com"]


def test_metadata_populates_official_domains_and_entity_aliases_without_network():
    metadata = build_instrument_metadata(
        {
            "holdings": [
                {"ticker": "SOFI", "group": "Account"},
                {"ticker": "WNUC.DE", "group": "Account"},
            ]
        },
        market_rows=[
            {"Ticker": "SOFI", "Name": "SoFi Technologies, Inc."},
            {"Ticker": "WNUC.DE", "Name": "WisdomTree Uranium and Nuclear Energy UCITS ETF - USD Acc"},
        ],
        enrich=False,
    )
    assert "investors.sofi.com" in metadata["SOFI"]["official_domains"]
    assert "SoFi" in metadata["SOFI"]["entity_aliases"]
    assert metadata["WNUC.DE"]["theme"] == "Uranium & Nuclear Energy"
    assert "uranium" in metadata["WNUC.DE"]["key_drivers"]


def test_official_equity_result_reaches_primary_entity_threshold():
    meta = {
        "name": "SoFi Technologies, Inc.",
        "instrument_type": "EQUITY",
        "entity_aliases": ["SoFi", "SoFi Technologies"],
        "official_domains": ["sofi.com", "investors.sofi.com"],
    }
    result = resolve_primary_entity(
        {
            "title": "SoFi Announces Date for Q2 2026 Financial Results",
            "summary": "SoFi Technologies will publish its quarterly results.",
            "url": "https://investors.sofi.com/news/news-details/2026/q2-results-date/default.aspx",
        },
        "SOFI",
        meta,
    )
    assert result["entity_role"] == "primary"
    assert result["domain_entity_match"] is True
    assert result["primary_entity_score"] >= 0.70


def test_etf_theme_event_can_be_primary_without_product_ticker_in_title():
    meta = {
        "name": "WisdomTree Uranium and Nuclear Energy UCITS ETF",
        "instrument_type": "ETF",
        "entity_aliases": ["WisdomTree Uranium and Nuclear Energy", "WNUC"],
        "theme": "Uranium & Nuclear Energy",
        "underlying_index": "WisdomTree Uranium and Nuclear Energy Index",
        "key_drivers": ["uranium", "nuclear energy", "uranium supply"],
        "official_domains": ["wisdomtree.eu"],
    }
    result = resolve_primary_entity(
        {
            "title": "Uranium supply outage tightens the nuclear fuel market",
            "summary": "Uranium supply remains constrained as nuclear energy demand expands.",
            "url": "https://www.reuters.com/markets/commodities/uranium-supply-outage-2026-07-17/",
        },
        "WNUC.DE",
        meta,
    )
    assert result["page_classification"] == "theme_event"
    assert result["entity_role"] == "primary"
    assert result["theme_title_match"] is True
    assert result["primary_entity_score"] >= 0.70


def test_gap_query_uses_official_domain_for_official_first_need():
    from daily_report.src.stock_daily_agent.research_gap_analyzer import _deterministic_gap_queries

    queries = _deterministic_gap_queries([
        {
            "ticker": "SOFI",
            "name": "SoFi Technologies, Inc.",
            "official_domains": ["investors.sofi.com"],
            "missing_needs": ["earnings_date"],
        }
    ])
    assert queries[0]["lane"] == "official"
    assert queries[0]["query"].startswith('site:investors.sofi.com ')


def test_gap_query_uses_theme_context_for_etf_driver_need():
    from daily_report.src.stock_daily_agent.research_gap_analyzer import _deterministic_gap_queries

    queries = _deterministic_gap_queries([
        {
            "ticker": "WNUC.DE",
            "name": "WisdomTree Uranium and Nuclear Energy UCITS ETF",
            "theme": "Uranium & Nuclear Energy",
            "underlying_index": "WisdomTree Uranium and Nuclear Energy Index",
            "key_drivers": ["uranium"],
            "missing_needs": ["theme_supply"],
        }
    ])
    assert queries[0]["lane"] == "theme"
    assert "WisdomTree Uranium and Nuclear Energy Index" in queries[0]["query"]
    assert "WNUC.DE" not in queries[0]["query"]


def test_official_subdomain_is_classified_as_official():
    from daily_report.src.stock_daily_agent.research_core.source_classifier import classify_source_quality

    result = classify_source_quality(
        {
            "url": "https://newsroom.investors.sofi.com/press-release/q2-results",
            "title": "SoFi Announces Q2 Results",
            "raw_snippet": "Press release",
        },
        meta={"official_domains": ["sofi.com"]},
    )
    assert result["source_type"] == "official"
