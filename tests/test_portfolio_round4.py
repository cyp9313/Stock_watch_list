# -*- coding: utf-8 -*-
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from daily_report.report_components import render_action_detail, render_news_group
from daily_report.scripts.build_portfolio_report import _to_berlin, build_html
from daily_report.src.stock_daily_agent.portfolio_research import (
    PortfolioResearchService, _evidence_kind, build_evidence_notes,
)
from daily_report.src.stock_daily_agent.portfolio_agent_runner import _validate_agent_advice
from daily_report.src.stock_daily_agent.portfolio_context import PortfolioRunContext
from daily_report.src.stock_daily_agent.research_service import ResearchService
from daily_report.portfolio_service import _extract_runner_failure
from daily_report.report_charts import svg_cumulative_returns
from daily_report.run_portfolio_report import _data_cutoffs, _enforce_observation_mode
from daily_report.run_portfolio_report import run_pipeline
from portfolio_analysis.action_targets import apply_deterministic_action_targets
from portfolio_analysis.metrics import (
    _compute_aggregates,
    calculate_portfolio_beta,
    calculate_portfolio_metrics,
    calculate_relative_window_return,
    compute_portfolio_risk_score,
    drawdown_score,
)
from portfolio_analysis.report_quality import evaluate_report_quality, PortfolioReportQualityError
from portfolio_analysis.metric_contracts import evidence_verification_score
from portfolio_analysis.rules import generate_portfolio_rule_findings
from portfolio_analysis.validators import validate_portfolio_claims


def test_historical_portfolio_beta_uses_common_finite_observations():
    dates = pd.date_range("2026-01-02", periods=80, freq="B")
    benchmark = pd.Series(np.linspace(-0.01, 0.01, 80), index=dates)
    portfolio = benchmark * 1.4
    result = calculate_portfolio_beta(portfolio, benchmark, min_observations=60)
    assert result["status"] == "actual"
    assert result["observations"] == 80
    assert result["value"] == pytest.approx(1.4)


def test_drawdown_score_and_missing_component_normalization():
    assert drawdown_score(-5) == 0
    assert drawdown_score(-15) == 5
    assert drawdown_score(-25) == 9
    assert drawdown_score(-40) == 13
    assert drawdown_score(-50) == 15
    risk = compute_portfolio_risk_score({
        "top1_weight": 0.25,
        "portfolio_beta": None,
        "annualized_volatility": 20,
        "max_drawdown_252d": -25,
        "weighted_high_correlation_exposure": 0.04,
        "technical_breadth": {"below_ema50_weight": 0.7},
        "aggregates": {"top_risk_contribution_sum": 0.65},
    })
    assert "beta" in risk["missing_components"]
    assert risk["score_confidence"] == pytest.approx(0.85)
    assert sum(risk["component_max"].values()) == 100


def test_relative_return_tolerates_one_sparse_holding_when_coverage_is_sufficient():
    dates = pd.date_range("2026-01-02", periods=80, freq="B")
    close = pd.DataFrame({
        "AAA": np.linspace(100, 120, 80),
        "BBB": [np.nan] * 30 + list(np.linspace(50, 55, 50)),
    }, index=dates)
    benchmark = pd.Series(np.linspace(100, 110, 80), index=dates)
    result = calculate_relative_window_return(
        close, pd.Series({"AAA": 0.95, "BBB": 0.05}), benchmark, 63,
        minimum_weight_coverage=0.90,
    )
    assert result["status"] == "actual"
    assert result["weight_coverage"] >= 0.90


def test_weekend_crypto_does_not_move_equity_or_benchmark_cutoff():
    dates = pd.to_datetime(["2026-07-16", "2026-07-17", "2026-07-18"])
    close = pd.DataFrame({
        "AAPL": [100, 101, np.nan],
        "BTC-EUR": [50000, 51000, 52000],
        "^GSPC": [6000, 6010, np.nan],
    }, index=dates)
    meta = {
        "AAPL": {"instrument_type": "EQUITY"},
        "BTC-EUR": {"instrument_type": "CRYPTO"},
    }
    result = _data_cutoffs(close, meta, "^GSPC")
    assert result["equity"] == "2026-07-17"
    assert result["crypto"] == "2026-07-18"
    assert result["benchmark"] == "2026-07-17"


def test_empty_theme_does_not_create_duplicate_exposure():
    snapshot = {
        "holdings": [{"ticker": "PPFB.DE", "weight": 0.5}, {"ticker": "SOFI", "weight": 0.5}],
        "data_quality": {},
    }
    metadata = {
        "PPFB.DE": {"instrument_type": "ETF", "theme": "", "underlying_index": ""},
        "SOFI": {"instrument_type": "EQUITY", "theme": "科技"},
    }
    findings = generate_portfolio_rule_findings(snapshot, {}, {}, instrument_metadata=metadata)
    assert not any(str(item.get("risk_id", "")).startswith("DUP_EXPOSURE") for item in findings)


def test_aggregates_carry_exact_top5_members_and_beta_threshold():
    rc = [
        {"ticker": f"T{i}", "weight": 0.1, "risk_contribution": value}
        for i, value in enumerate([0.20, 0.18, 0.16, 0.14, 0.12, 0.10], start=1)
    ]
    holdings = [{"ticker": f"T{i}", "weight": 1 / 6, "beta": 1.6} for i in range(1, 7)]
    result = _compute_aggregates(holdings, {"risk_contributions": rc})
    assert [x["ticker"] for x in result["top5_risk_contributors"]] == ["T1", "T2", "T3", "T4", "T5"]
    assert result["top_risk_contribution_sum"] == pytest.approx(sum(x["risk_contribution"] for x in rc[:5]))
    assert result["high_beta_weight"]["threshold"] == 1.5


def test_correlation_pairs_include_observations_and_minimum_sample():
    rng = np.random.default_rng(7)
    dates = pd.date_range("2026-01-02", periods=90, freq="B")
    base = 100 * np.cumprod(1 + rng.normal(0, 0.01, len(dates)))
    close = pd.DataFrame({"AAA": base, "BBB": base * 1.01, "^GSPC": base * 0.9}, index=dates)
    snapshot = {
        "benchmark": "^GSPC",
        "holdings": [
            {"ticker": "AAA", "weight": 0.5, "beta": 1.0},
            {"ticker": "BBB", "weight": 0.5, "beta": 1.0},
        ],
        "instrument_metadata": {},
    }
    metrics = calculate_portfolio_metrics(snapshot, close, benchmark="^GSPC")
    pair = metrics["high_correlation_pairs"][0]
    assert pair["observations"] >= 60
    assert pair["combined_weight"] == pytest.approx(1.0)


def test_no_news_quality_gate_blocks_publication(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_REPORT_REQUIRE_TOP_RISK_NEWS", "true")
    result = evaluate_report_quality(
        {}, {"portfolio_beta_status": "actual"},
        {"status": "not_configured", "evidence": [], "diagnostics": {"status": "not_configured", "top_risk_coverage": 0}},
        {"final_confidence": 0.8, "actions": []}, {},
    )
    assert result["publishable"] is False
    assert any("新闻" in message for message in result["blocking_errors"])


def test_partial_news_coverage_degrades_instead_of_blocking(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_REPORT_REQUIRE_TOP_RISK_NEWS", "true")
    monkeypatch.setenv("PORTFOLIO_REPORT_MIN_NEWS_COVERAGE", "0.60")
    monkeypatch.setenv("PORTFOLIO_REPORT_STRICT_NEWS_COVERAGE", "false")
    evidence = [{
        "evidence_id": "E001", "ticker": "AAA", "recency_tier": "fresh_event",
        "article_fetch_ok": False,
    }]
    result = evaluate_report_quality(
        {}, {"portfolio_beta_status": "actual"},
        {
            "status": "insufficient_coverage", "evidence": evidence,
            "diagnostics": {"status": "insufficient_coverage", "top_risk_coverage": 0.40},
        },
        {"final_confidence": 0.40, "actions": []}, {},
    )
    assert result["publishable"] is True
    assert result["actionable"] is False
    assert not result["blocking_errors"]
    assert any("40%" in message and "60%" in message for message in result["warnings"])


def test_partial_news_coverage_can_still_be_strict(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_REPORT_REQUIRE_TOP_RISK_NEWS", "true")
    monkeypatch.setenv("PORTFOLIO_REPORT_MIN_NEWS_COVERAGE", "0.60")
    monkeypatch.setenv("PORTFOLIO_REPORT_STRICT_NEWS_COVERAGE", "true")
    result = evaluate_report_quality(
        {}, {"portfolio_beta_status": "actual"},
        {
            "status": "insufficient_coverage",
            "evidence": [{"recency_tier": "fresh_event"}],
            "diagnostics": {"status": "insufficient_coverage", "top_risk_coverage": 0.40},
        },
        {"final_confidence": 0.40, "actions": []}, {},
    )
    assert result["publishable"] is False
    assert any("40%" in message and "60%" in message for message in result["blocking_errors"])


def test_non_actionable_report_converts_trades_to_watch():
    advice = {"actions": [{
        "ticker": "AAA", "action": "reduce", "current_weight": 0.2,
        "target_weight_min": 0.1, "target_weight_max": 0.15,
        "expected_portfolio_risk_reduction": 0.2,
    }]}
    action = _enforce_observation_mode(advice)["actions"][0]
    assert action["action"] == "watch"
    assert action["target_weight_min"] == action["target_weight_max"] == 0.2
    assert action["expected_portfolio_risk_reduction"] is None


def test_no_news_fails_before_main_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIO_REPORT_REQUIRE_TOP_RISK_NEWS", "true")
    dates = pd.date_range("2026-03-01", periods=80, freq="B")
    close = pd.DataFrame({"AAA": np.linspace(100, 120, 80), "^GSPC": np.linspace(100, 110, 80)}, index=dates)
    payload = {"portfolio_page": {
        "id": "p1", "name": "P", "analysis_settings": {"benchmark": "^GSPC", "base_currency": "USD"},
        "holdings": [{"group": "G", "ticker": "AAA", "buy_price": 90, "shares": 1, "buy_currency": "USD"}],
    }}
    market_rows = [{"Ticker": "AAA", "Name": "AAA Inc.", "Price": 120, "Currency": "USD", "RSI": 50}]

    class NoEvidence:
        def research(self, *args, **kwargs):
            return {"status": "no_raw_results", "evidence": [], "raw_results": [], "filtered_results": [],
                    "diagnostics": {"status": "no_raw_results", "top_risk_coverage": 0.0}}

    called = {"agent": False}
    def agent(*args, **kwargs):
        called["agent"] = True
        return {"report_mode": "ai", "actions": []}

    with pytest.raises(PortfolioReportQualityError):
        run_pipeline(
            payload, run_dir=tmp_path, output=tmp_path / "report.html", portfolio_name="P",
            portfolio_id="p1", owner_scope="owner", model="qwen-plus", provider="dashscope",
            search_provider="none", close=close, market_rows=market_rows, fx_rates={},
            research_service=NoEvidence(), agent_runner=agent, verbose=False,
        )
    assert called["agent"] is False


def test_partial_news_coverage_reaches_main_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTFOLIO_REPORT_REQUIRE_TOP_RISK_NEWS", "true")
    monkeypatch.setenv("PORTFOLIO_REPORT_STRICT_NEWS_COVERAGE", "false")
    monkeypatch.setenv("PORTFOLIO_REPORT_ALLOW_QUANT_FALLBACK", "false")
    monkeypatch.setattr(
        "daily_report.run_portfolio_report.summarize_evidence_zh",
        lambda evidence, *args, **kwargs: {"status": "success", "evidence": evidence, "errors": []},
    )
    dates = pd.date_range("2026-03-01", periods=80, freq="B")
    close = pd.DataFrame({"AAA": np.linspace(100, 120, 80), "^GSPC": np.linspace(100, 110, 80)}, index=dates)
    payload = {"portfolio_page": {
        "id": "p1", "name": "P", "analysis_settings": {"benchmark": "^GSPC", "base_currency": "USD"},
        "holdings": [{"group": "G", "ticker": "AAA", "buy_price": 90, "shares": 1, "buy_currency": "USD"}],
    }}
    market_rows = [{"Ticker": "AAA", "Name": "AAA Inc.", "Price": 120, "Currency": "USD", "RSI": 50}]

    class PartialEvidence:
        def research(self, *args, **kwargs):
            evidence = [{
                "evidence_id": "E001", "ticker": "AAA", "related_tickers": ["AAA"],
                "recency_tier": "fresh_event", "published_date": "2026-07-17",
                "source_quality": "tier_1", "article_fetch_ok": False,
            }]
            return {
                "status": "insufficient_coverage", "evidence": evidence,
                "raw_results": evidence, "filtered_results": evidence,
                "diagnostics": {
                    "status": "insufficient_coverage", "top_risk_coverage": 0.40,
                    "raw_results_count": 1, "filtered_results_count": 1,
                    "selected_evidence_count": 1,
                },
            }

    called = {"agent": False}

    def agent(*args, **kwargs):
        called["agent"] = True
        raise RuntimeError("agent-called")

    with pytest.raises(RuntimeError, match="agent-called"):
        run_pipeline(
            payload, run_dir=tmp_path, output=tmp_path / "report.html", portfolio_name="P",
            portfolio_id="p1", owner_scope="owner", model="qwen-plus", provider="dashscope",
            search_provider="serper", close=close, market_rows=market_rows, fx_rates={},
            research_service=PartialEvidence(), agent_runner=agent, verbose=False,
        )
    assert called["agent"] is True


def test_worker_does_not_retry_quality_gate_failure(monkeypatch):
    import daily_report.worker as worker
    monkeypatch.setattr(worker, "claim_next_job", lambda: {
        "id": "a" * 32, "report_kind": "portfolio", "subject_name": "P", "ticker": "",
        "recipient_masked": "u***@example.com", "attempts": 1, "max_attempts": 3,
        "report_html": None,
    })
    monkeypatch.setattr(worker, "generate_job_report", lambda job: {
        "success": False, "quality_gate_failed": True, "error": "quality gate failed",
    })
    recorded = {}
    def mark_failure(job_id, error, *, retry=True):
        recorded.update({"job_id": job_id, "error": error, "retry": retry})
        return "failed"
    monkeypatch.setattr(worker, "mark_job_failure", mark_failure)
    assert worker.process_one_job() is True
    assert recorded["retry"] is False


def test_research_service_returns_structured_not_configured(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    result = ResearchService().search(["AAA latest news"])
    assert result["results"] == []
    assert result["diagnostics"]["status"] == "not_configured"


def test_evidence_selection_enforces_per_ticker_quota(monkeypatch):
    monkeypatch.setattr(
        "daily_report.src.stock_daily_agent.portfolio_research._fetch_article_text",
        lambda *args, **kwargs: {"ok": False},
    )
    candidates = []
    for ticker in ("AAA", "BBB"):
        for index in range(6):
            candidates.append({
                "ticker": ticker, "scope": "ticker", "title": f"{ticker} event {index}",
                "summary": f"{ticker} reported a material event with details number {index}.",
                "url": f"https://example.com/{ticker}/{index}", "source": "Reuters",
                "published_date": "2026-07-17", "event_hint": "event",
            })
    evidence = build_evidence_notes(candidates, {"AAA": {}, "BBB": {}}, max_articles=0, max_evidence=15)
    counts = {ticker: sum(1 for item in evidence if item.get("ticker") == ticker) for ticker in ("AAA", "BBB")}
    assert max(counts.values()) <= 3
    assert min(counts.values()) >= 1


def test_serper_search_applies_real_date_range_and_domain_source(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"organic": [{
                "title": "Tesla reports quarterly deliveries",
                "link": "https://ir.tesla.com/press-release",
                "snippet": "Tesla reported quarterly deliveries.",
                "date": "2026-07-01",
            }]}

    def fake_post(url, *, headers, json, timeout):
        captured.update({"url": url, "json": json})
        return Response()

    monkeypatch.setattr("daily_report.src.stock_daily_agent.research_service.requests.post", fake_post)
    result = ResearchService().search(
        ["Tesla latest news"], provider="serper", max_results=3, recency_days=120,
    )
    assert " after:" in captured["json"]["q"]
    assert result["results"][0]["source"] == "ir.tesla.com"


def test_reference_homepage_is_not_event_news_and_verified_body_is_used(monkeypatch):
    assert _evidence_kind(
        {"ticker": "TSLA", "url": "https://ir.tesla.com/", "title": "Tesla Investor Relations"},
        "ticker", {"instrument_type": "EQUITY"},
    ) == "reference"

    class Search:
        def search(self, queries, **kwargs):
            query = queries[0]
            results = []
            if "Tesla" in query:
                results = [
                    {
                        "title": "Tesla Investor Relations", "url": "https://ir.tesla.com/",
                        "summary": "Tesla mission statement", "published_date": "",
                        "source": "ir.tesla.com", "query": query,
                    },
                    {
                        "title": "Tesla reports quarterly delivery update",
                        "url": "https://www.reuters.com/business/autos-transportation/tesla-deliveries-2026-07-01/",
                        "summary": "Tesla reported a quarterly delivery update.",
                        "published_date": "2026-07-01", "source": "Reuters", "query": query,
                    },
                ]
            return {"results": results, "diagnostics": {"provider_used": "serper", "errors": []}}

    monkeypatch.setattr(
        "daily_report.src.stock_daily_agent.portfolio_research._fetch_article_text",
        lambda *args, **kwargs: {
            "ok": True,
            "article_text_quality_ok": True,
            "text": "Tesla reported its latest quarterly delivery figures and provided an operational update.",
        },
    )
    service = PortfolioResearchService(provider="serper")
    service._service = Search()
    result = service.research(
        ["TSLA"], {"TSLA": {"name": "Tesla Inc.", "instrument_type": "EQUITY"}},
        max_evidence=3,
    )
    assert result["diagnostics"]["rejected"]["reference_page"] >= 1
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["content_basis"] == "article_body"
    assert "quarterly delivery figures" in " ".join(result["evidence"][0]["facts"])


def test_concrete_dated_search_snippet_is_usable_but_unverified(monkeypatch):
    class Search:
        def search(self, queries, **kwargs):
            query = queries[0]
            return {
                "results": [{
                    "title": "Tesla reports quarterly deliveries and updates outlook",
                    "url": "https://www.reuters.com/business/autos-transportation/tesla-deliveries-2026-07-17/",
                    "summary": (
                        "Tesla reported quarterly vehicle deliveries on Friday and updated "
                        "its production outlook after publishing the latest operating figures."
                    ),
                    "published_date": "2026-07-17",
                    "source": "Reuters",
                    "query": query,
                }],
                "diagnostics": {"provider_used": "serper", "errors": []},
            }

    monkeypatch.setattr(
        "daily_report.src.stock_daily_agent.portfolio_research._fetch_article_text",
        lambda *args, **kwargs: {"ok": False, "error": "paywall"},
    )
    service = PortfolioResearchService(provider="serper")
    service._service = Search()
    result = service.research(
        ["TSLA"], {"TSLA": {"name": "Tesla Inc.", "instrument_type": "EQUITY"}},
        max_evidence=3,
    )
    assert result["status"] == "success"
    assert len(result["evidence"]) == 1
    item = result["evidence"][0]
    assert item["article_fetch_ok"] is False
    assert item["content_basis"] == "search_snippet_unverified"
    assert item["confidence"] <= 0.60
    assert result["diagnostics"]["snippet_fallback_count"] == 1


def test_evidence_verification_score_penalizes_unverified_snippets():
    assert evidence_verification_score([]) == 0.3
    assert evidence_verification_score([{"article_fetch_ok": False}]) == 0.5
    assert evidence_verification_score([
        {"article_fetch_ok": True}, {"article_fetch_ok": False},
    ]) == 0.75


def test_news_card_visibly_marks_unverified_search_snippet():
    html = render_news_group("TSLA", [{
        "title": "Tesla event", "source_name": "Reuters", "published_date": "2026-07-17",
        "ticker": "TSLA", "source_quality": "tier_1", "article_fetch_ok": False,
    }])
    assert "搜索摘要·未验证" in html


def test_news_card_renders_verified_article_with_existing_theme_color():
    html = render_news_group("TSLA", [{
        "title": "Tesla verified event", "source_name": "Reuters",
        "published_date": "2026-07-17", "ticker": "TSLA",
        "source_quality": "tier_1", "article_fetch_ok": True,
    }])
    assert "正文已验证" in html
    assert "#3fb950" in html


def test_cumulative_chart_legend_is_moved_away_from_title():
    svg = svg_cumulative_returns(
        ["2026-07-17", "2026-07-18"], [0.0, 1.0], [0.0, 0.5],
    )
    assert '<rect x="700" y="6"' in svg
    assert '<rect x="40" y="6" width="12"' not in svg


def test_action_risk_reduction_is_python_calculated():
    advice = {"actions": [{"ticker": "AAA", "action": "reduce", "current_weight": 0.6}]}
    metrics = {
        "risk_contributions": [
            {"ticker": "AAA", "weight": 0.6, "risk_contribution": 0.8},
            {"ticker": "BBB", "weight": 0.4, "risk_contribution": 0.2},
        ],
        "covariance_tickers": ["AAA", "BBB"],
        "covariance_matrix_daily": [[0.0004, 0.0], [0.0, 0.0001]],
    }
    result = apply_deterministic_action_targets(advice, metrics, {})["actions"][0]
    assert result["expected_portfolio_risk_reduction"] is not None
    assert result["expected_risk_change"]["method"] == "target_midpoint_to_cash_same_covariance"


def test_agent_validation_applies_python_targets_and_caps_uncited_confidence(tmp_path):
    ctx = PortfolioRunContext(
        run_dir=tmp_path,
        portfolio_name="P",
        portfolio_id="p1",
        owner_scope="owner",
        base_currency="EUR",
        benchmark="^GSPC",
        model="qwen-plus",
        provider="dashscope",
        search_provider="none",
        snapshot={"holdings": [{"ticker": "WNUC.DE", "weight": 0.0646}]},
        metrics={"risk_contributions": [{
            "ticker": "WNUC.DE", "weight": 0.0646, "risk_contribution": 0.10,
        }]},
        ranking={"items": [], "top_risk_tickers": ["WNUC.DE"]},
        evidence=[],
        instrument_metadata={},
        settings={},
        output_html=tmp_path / "report.html",
        advice_json_path=tmp_path / "advice.json",
    )
    advice = {
        "actions": [{
            "ticker": "WNUC.DE",
            "action": "reduce",
            "target_weight_min": 0.06,
            "target_weight_max": 0.0646,
            "confidence": 0.79,
        }],
    }
    action = _validate_agent_advice(advice, ctx)["actions"][0]
    assert action["target_weight_max"] <= 0.0646 * 0.8 + 1e-6
    assert action["model_confidence"] == pytest.approx(0.79)
    assert action["confidence"] == pytest.approx(0.3)


def test_agent_caps_unverified_fresh_evidence_at_sixty_percent(tmp_path):
    ctx = PortfolioRunContext(
        run_dir=tmp_path, portfolio_name="P", portfolio_id="p1", owner_scope="owner",
        base_currency="EUR", benchmark="^GSPC", model="qwen-plus", provider="dashscope",
        search_provider="serper",
        snapshot={"holdings": [{"ticker": "AAA", "weight": 0.5}]},
        metrics={"risk_contributions": [{"ticker": "AAA", "weight": 0.5, "risk_contribution": 0.5}]},
        ranking={"items": [], "top_risk_tickers": ["AAA"]},
        evidence=[{
            "evidence_id": "E001", "ticker": "AAA", "recency_tier": "fresh_event",
            "article_fetch_ok": False, "content_basis": "search_snippet_unverified",
        }],
        instrument_metadata={}, settings={}, output_html=tmp_path / "report.html",
        advice_json_path=tmp_path / "advice.json",
    )
    advice = {"actions": [{
        "ticker": "AAA", "action": "reduce", "current_weight": 0.5,
        "target_weight_min": 0.3, "target_weight_max": 0.4,
        "confidence": 0.85, "evidence_ids": ["E001"],
    }]}
    action = _validate_agent_advice(advice, ctx)["actions"][0]
    assert action["model_confidence"] == pytest.approx(0.85)
    assert action["confidence"] == pytest.approx(0.60)


def test_agent_sanitizes_internal_terms_and_cross_ticker_key_risk_evidence(tmp_path):
    ctx = PortfolioRunContext(
        run_dir=tmp_path, portfolio_name="P", portfolio_id="p1", owner_scope="owner",
        base_currency="EUR", benchmark="^GSPC", model="qwen-plus", provider="dashscope",
        search_provider="serper",
        snapshot={"holdings": [{"ticker": "AAA", "weight": 0.5, "rsi_regime": "weak"}]},
        metrics={"risk_contributions": []}, ranking={"items": [], "top_risk_tickers": ["AAA"]},
        evidence=[{
            "evidence_id": "E005", "ticker": "BBB", "related_tickers": ["BBB"],
            "recency_tier": "fresh_event", "article_fetch_ok": False,
        }],
        instrument_metadata={}, settings={}, output_html=tmp_path / "report.html",
        advice_json_path=tmp_path / "advice.json",
    )
    result = _validate_agent_advice({
        "executive_summary": ["AAA 的 rsi_regime 为 weak。"],
        "key_risks": [{
            "risk_id": "R002", "title": "AAA 风险", "description": "E005 显示 AAA 需要观察。",
            "affected_tickers": ["AAA"], "evidence_ids": ["E005"],
        }],
        "actions": [{
            "ticker": "AAA", "action": "watch", "confidence": 0.5,
            "technical_reason": "AAA 当前 weak。",
            "news_reason": "内容依据 content_basis=search_snippet_unverified。",
        }],
    }, ctx)
    assert result["key_risks"][0]["evidence_ids"] == []
    assert "E005" not in result["key_risks"][0]["description"]
    rendered_text = str(result)
    assert "search_snippet_unverified" not in rendered_text
    assert "rsi_regime" not in rendered_text
    assert " weak" not in rendered_text.lower()
    assert any("R002" in warning and "E005" in warning for warning in result["validation_warnings"])


def test_portfolio_service_exposes_agent_validation_failure():
    message, quality_failure = _extract_runner_failure(
        "Traceback\nPortfolio Agent validation failed: WNUC.DE 高置信度操作没有新鲜证据支撑。\n"
    )
    assert quality_failure is True
    assert "严格校验" in message
    assert "WNUC.DE" in message


def test_claim_validation_blocks_wrong_rsi_and_internal_tokens():
    advice = {
        "executive_summary": ["SOFI 当前深度超卖，portfolio_risk_score=38。"],
        "actions": [],
    }
    errors, _ = validate_portfolio_claims(
        advice, {"holdings": [{"ticker": "SOFI", "rsi_regime": "neutral"}]}, {}, [],
    )
    assert any("RSI" in error for error in errors)
    assert any("portfolio_risk_score" in error for error in errors)


def test_metric_evidence_and_chinese_html_do_not_leak_internal_reallocation_enum():
    detail = render_action_detail(
        {"ticker": "AAA", "action": "watch", "metric_evidence": [{"ticker": "AAA", "metric": "risk_contribution"}]},
        ticker_metrics={"risk_contribution": 0.1225},
    )
    assert "12.25%" in detail
    html = build_html(
        {"portfolio_name": "P", "holdings": [], "summary": {}, "data_quality": {}},
        {}, {"items": []},
        {"actions": [], "portfolio_reallocation": {"destination": "cash_unspecified"}}, [],
    )
    assert "cash_unspecified" not in html
    assert html.count("本报告仅供研究参考，不构成投资建议。") == 1


def test_html_explains_confidence_limiter():
    html = build_html(
        {"portfolio_name": "P", "holdings": [], "summary": {}, "data_quality": {}},
        {}, {"items": []},
        {
            "actions": [], "confidence": 0.333, "final_confidence": 0.333,
            "confidence_components": {
                "model_confidence": 0.8,
                "data_quality": 1.0,
                "metadata_coverage": 0.9,
                "evidence_coverage": 0.6,
                "evidence_freshness": 0.333,
                "evidence_verification": 0.5,
            },
        },
        [],
    )
    assert "报告置信度分解" in html
    assert "证据新鲜度" in html
    assert "证据正文验证" in html
    assert "限制项" in html


def test_html_displays_degraded_news_coverage_warning():
    html = build_html(
        {"portfolio_name": "P", "holdings": [], "summary": {}, "data_quality": {}},
        {}, {"items": [], "top_risk_tickers": []}, {"actions": []}, [],
        research_diagnostics={"status": "insufficient_coverage", "top_risk_coverage": 0.4},
        report_quality={
            "quality_score": 0.4, "actionable": False,
            "warnings": ["Top-risk 新闻覆盖率 40% 低于目标 60%；报告允许降级生成"],
        },
    )
    assert "质量提示" in html
    assert "新闻覆盖率 40% 低于目标 60%" in html


def test_to_berlin_accepts_naive_datetime():
    rendered = _to_berlin("2026-07-18T12:00:00")
    assert "Europe/Berlin" in rendered
