from __future__ import annotations

from datetime import datetime

from daily_report.report_charts import svg_cumulative_returns
from daily_report.scripts.build_portfolio_report import build_html
from daily_report.src.stock_daily_agent.evidence_summarizer import _apply_summaries_by_uid
from daily_report.src.stock_daily_agent.portfolio_research import _normalize_date, _apply_materiality_and_clustering
from portfolio_analysis.research_diagnostics import evidence_stage_diagnostics


def test_relative_hour_date_is_normalized_and_unknown_text_is_not_truncated():
    ref = datetime.fromisoformat("2026-07-19T21:20:00+02:00")
    assert _normalize_date("21 hours ago", reference_datetime=ref) == "2026-07-19"
    assert _normalize_date("25 hours ago", reference_datetime=ref) == "2026-07-18"
    assert _normalize_date("not a provider date", reference_datetime=ref) == ""


def test_materiality_ranking_persists_official_source_classification():
    evidence = [{
        "evidence_uid": "ev_sofi",
        "ticker": "SOFI",
        "title": "SoFi Announces Date for Q2 2026 Financial Results",
        "summary": "SoFi Technologies will report quarterly results on July 28, 2026.",
        "url": "https://investors.sofi.com/news/news-details/2026/q2-results-date/default.aspx",
        "published_date": "2026-07-19",
        "event_hint": "earnings_date",
        "lane": "official",
        "entity_role": "primary",
    }]
    metadata = {
        "SOFI": {
            "name": "SoFi Technologies, Inc.",
            "instrument_type": "EQUITY",
            "entity_aliases": ["SoFi", "SoFi Technologies"],
            "official_domains": ["investors.sofi.com"],
        }
    }
    ranked, stats, _ = _apply_materiality_and_clustering(
        evidence,
        instrument_metadata=metadata,
        ranking={"top_risk_tickers": ["SOFI"]},
        metrics={"risk_contributions": [{"ticker": "SOFI", "risk_contribution": 1.0}]},
    )
    assert ranked[0]["source_type"] == "official"
    assert ranked[0]["source_is_official"] is True
    assert sum(stats["source_type_counts"]["official"].values()) == 1


def test_summarizer_records_per_item_isolation_reason():
    evidence = [
        {
            "evidence_uid": "ev_1",
            "ticker": "SOFI",
            "title": "One",
            "raw_title": "One",
            "url": "https://example.com/1",
            "source_domain": "example.com",
            "published_date": "2026-07-19",
            "event_key": "one",
        },
        {
            "evidence_uid": "ev_2",
            "ticker": "TSLA",
            "title": "Two",
            "raw_title": "Two",
            "url": "https://example.com/2",
            "source_domain": "example.com",
            "published_date": "2026-07-19",
            "event_key": "two",
        },
    ]
    errors = _apply_summaries_by_uid(
        evidence,
        [{"evidence_uid": "ev_1", "event_title_zh": "标题但没有 accept"}],
    )
    assert "summarizer_missing_accept:ev_1" in errors
    assert any(error.startswith("summarizer_missing_uids:ev_2") for error in errors)
    assert evidence[0]["summary_isolation_reason"] == "missing_or_invalid_accept"
    assert evidence[1]["summary_isolation_reason"] == "missing_output"
    assert evidence[0]["accept"] is False
    assert evidence[1]["accept"] is False


def test_stage_diagnostics_distinguish_materiality_summary_and_final_gate():
    evidence = [
        {"ticker": "A", "source_type": "official", "lane": "official", "materiality_accepted": False},
        {
            "ticker": "B", "source_type": "major_media", "lane": "news",
            "materiality_accepted": True, "summary_isolation_reason": "missing_output", "accept": False,
        },
        {
            "ticker": "C", "source_type": "official", "lane": "official",
            "materiality_accepted": True, "summary_integrity_ok": True, "accept": False,
            "reject_reason": "low_materiality",
        },
        {
            "ticker": "D", "source_type": "official", "lane": "official",
            "materiality_accepted": True, "summary_integrity_ok": True, "accept": True,
            "evidence_id": "E001",
        },
        {
            "ticker": "E", "source_type": "unknown", "lane": "theme",
            "materiality_accepted": True, "summary_integrity_ok": True, "accept": True,
            "recency_tier": "unknown",
        },
    ]
    diag = evidence_stage_diagnostics(evidence)
    totals = diag["evidence_stage_totals"]
    assert totals["materiality_rejected"] == 1
    assert totals["summary_isolated"] == 1
    assert totals["summarizer_rejected"] == 1
    assert totals["accepted"] == 1
    assert totals["final_gate_rejected"] == 1
    assert diag["summarizer_isolation_reasons"] == {"missing_output": 1}
    assert diag["evidence_stage_by_source_type"]["official"]["accepted"] == 1


def _snapshot() -> dict:
    return {
        "portfolio_name": "Portfolio",
        "report_date": "2026-07-19",
        "as_of": "2026-07-19T21:20:00+02:00",
        "base_currency": "EUR",
        "benchmark": "^GSPC",
        "summary": {"total_market_value_base": 1000.0},
        "data_quality": {},
        "holdings": [{
            "ticker": "SOFI", "weight": 1.0, "market_value_base": 1000.0,
            "price_vs_ema50_pct": -2.0,
        }],
        "run_timeline": {
            "snapshot_completed_at": "2026-07-19T21:20:00+02:00",
            "news_search_completed_at": "2026-07-19T21:23:00+02:00",
            "report_rendered_at": "2026-07-19T21:24:00+02:00",
        },
        "data_cutoffs": {"equity": "2026-07-17", "etf": "2026-07-17", "benchmark": "2026-07-17"},
    }


def test_observation_report_renders_diagnostics_and_simplified_watch_details():
    snapshot = _snapshot()
    metrics = {
        "portfolio_risk_score": 54,
        "portfolio_risk_level": "medium_high",
        "risk_score_confidence": 0.85,
        "risk_score_component_max": {},
        "relative_returns": {},
        "risk_contributions": [{"ticker": "SOFI", "risk_contribution": 1.0}],
        "aggregates": {},
    }
    advice = {
        "report_mode": "quantitative_fallback",
        "observation_only": True,
        "portfolio_stance": "observe",
        "risk_level": "medium_high",
        "confidence": 0.4,
        "final_confidence": 0.0,
        "confidence_components": {},
        "executive_summary": ["确定性观察。"],
        "key_risks": [],
        "actions": [{
            "ticker": "SOFI", "action": "watch", "priority": 1,
            "current_weight": 1.0, "target_weight_min": 1.0, "target_weight_max": 1.0,
            "confidence": 0.0, "portfolio_reason": "风险贡献较高。",
            "technical_reason": "位于 EMA50 下方。", "news_reason": "没有 Accepted Evidence。",
            "bull_case": "不应展示", "bear_case": "不应展示",
            "monitoring_items": ["风险贡献"],
        }],
        "portfolio_analysis": {}, "watch_items": [], "data_limitations": [],
    }
    diagnostics = {
        "status": "insufficient_coverage",
        "raw_results_count": 20,
        "filtered_results_count": 5,
        "selected_evidence_count": 3,
        "accepted_evidence_count": 0,
        "latest_selected_event_date": "21 hours ago",
        "materiality_stats": {"rejected_count": 1, "accepted_count": 2, "rejected_reasons": {}},
        "summarizer_isolation_count": 2,
        "summarizer_isolation_reasons": {"missing_output": 1, "missing_or_invalid_accept": 1},
        "summarizer_isolated_items": [{
            "ticker": "SOFI", "source_type": "official", "lane": "official",
            "reason": "missing_output", "title": "SoFi event",
        }],
        "summarizer_rejected_count": 0,
        "final_gate_rejected_count": 0,
        "evidence_stage_by_source_type": {
            "official": {"materiality_passed": 2, "summary_isolated": 2, "accepted": 0},
        },
        "evidence_stage_by_ticker": {
            "SOFI": {"materiality_passed": 2, "summary_isolated": 2, "accepted": 0},
        },
        "evidence_stage_by_lane": {
            "official": {"materiality_passed": 2, "summary_isolated": 2, "accepted": 0},
        },
    }
    html = build_html(
        snapshot, metrics, {"top_risk_tickers": ["SOFI"]}, advice, [],
        settings={"risk_profile": "growth", "investment_horizon": "12m+"},
        research_diagnostics=diagnostics,
        report_quality={"publishable": True, "observation_only": True, "quality_score": 0.0},
        rejected_evidence=[{}, {}, {}],
    )
    assert "最新候选事件：未知" in html
    assert "21 hours a" not in html
    assert "摘要隔离原因" in html
    assert "模型漏返回该 Evidence UID 1" in html
    assert "按来源类型的研究阶段统计" in html
    assert "按标的的研究阶段统计" in html
    assert "按 Search Lane 的研究阶段统计" in html
    assert "重点观察清单（按综合风险优先级排序）" in html
    assert "不等同于单纯按风险贡献排序的 Top5" in html
    assert "升级为可操作建议所需条件" in html
    assert "多头情景" not in html
    assert "空头情景" not in html
    assert "进一步减仓条件" not in html
    assert "目标区间" not in html


def test_cumulative_chart_strips_midnight_timestamp_from_axis_labels():
    svg = svg_cumulative_returns(
        ["2026-07-17 00:00:00", "2026-07-18 00:00:00"],
        [0.0, 1.0],
        [0.0, 0.5],
    )
    assert "2026-07-17" in svg
    assert "00:00:00" not in svg
