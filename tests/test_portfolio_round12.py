from __future__ import annotations

from portfolio_analysis.research_diagnostics import (
    evidence_stage_diagnostics,
    merge_evidence_by_identity,
    refresh_research_stage_diagnostics,
)
from daily_report.scripts.build_portfolio_report import build_html
from daily_report.src.stock_daily_agent.portfolio_research import build_evidence_notes
from daily_report.src.stock_daily_agent.research_core.evidence_id import (
    evidence_final_gate_reasons,
    finalize_evidence_ids,
)


def test_cross_lane_duplicate_article_is_counted_once_and_stage_totals_close():
    evidence = [
        {
            "evidence_uid": "ev_news",
            "ticker": "SOFI",
            "url": "https://investors.sofi.com/news/q2?utm_source=news",
            "title": "SoFi Q2 date",
            "source_type": "official",
            "lane": "news",
            "materiality_accepted": False,
        },
        {
            "evidence_uid": "ev_official",
            "ticker": "SOFI",
            "url": "https://investors.sofi.com/news/q2",
            "title": "SoFi announces Q2 date",
            "source_type": "official",
            "lane": "official",
            "materiality_accepted": False,
        },
    ]
    diag = evidence_stage_diagnostics(evidence)
    assert diag["selected_evidence_count"] == 1
    assert diag["diagnostic_duplicate_candidate_count"] == 1
    assert diag["evidence_stage_totals"]["materiality_rejected"] == 1
    assert diag["stage_terminal_count"] == 1
    assert diag["stage_count_invariant_ok"] is True


def test_merge_evidence_uses_url_identity_not_only_uid():
    merged = merge_evidence_by_identity(
        [{
            "evidence_uid": "ev_first",
            "ticker": "SOFI",
            "url": "https://investors.sofi.com/news/q2?utm_campaign=x",
            "materiality_accepted": False,
            "selection_score": 0.3,
        }],
        [{
            "evidence_uid": "ev_gap",
            "ticker": "SOFI",
            "url": "https://investors.sofi.com/news/q2",
            "materiality_accepted": True,
            "selection_score": 0.8,
        }],
    )
    assert len(merged) == 1
    assert merged[0]["materiality_accepted"] is True
    assert merged[0]["evidence_uid"] == "ev_gap"


def test_refresh_research_diagnostics_replaces_stale_materiality_counts():
    result = {
        "diagnostics": {
            "selected_evidence_count": 15,
            "materiality_stats": {"accepted_count": 1, "rejected_count": 14},
        }
    }
    evidence = [
        {
            "evidence_uid": "ev_a",
            "ticker": "SOFI",
            "url": "https://example.com/a",
            "published_date": "2026-07-19",
            "source_type": "official",
            "lane": "official",
            "materiality_accepted": False,
        },
        {
            "evidence_uid": "ev_b",
            "ticker": "SOFI",
            "url": "https://example.com/b",
            "published_date": "2026-07-18",
            "source_type": "official",
            "lane": "official",
            "materiality_accepted": True,
            "summary_integrity_ok": True,
            "accept": True,
            "entity_role": "primary",
            "recency_tier": "fresh_event",
            "article_fetch_ok": True,
        },
    ]
    finalize_evidence_ids(evidence)
    refresh_research_stage_diagnostics(result, evidence)
    diag = result["diagnostics"]
    assert diag["selected_evidence_count"] == 2
    assert diag["materiality_stats"]["accepted_count"] == 1
    assert diag["materiality_stats"]["rejected_count"] == 1
    assert diag["stage_count_invariant_ok"] is True
    assert diag["latest_selected_event_date"] == "2026-07-19"


def test_relative_date_uses_per_result_retrieval_timestamp():
    notes = build_evidence_notes(
        [{
            "ticker": "SOFI",
            "scope": "ticker",
            "title": "SoFi announces earnings date",
            "summary": "SoFi Technologies will report quarterly results on a stated date.",
            "url": "https://investors.sofi.com/news/q2",
            "source": "SoFi Investor Relations",
            "published_date": "21 hours ago",
            "search_retrieved_at": "2026-07-19T22:02:58+02:00",
            "lane": "official",
            "event_hint": "earnings_date",
        }],
        {
            "SOFI": {
                "name": "SoFi Technologies",
                "instrument_type": "EQUITY",
                "official_domains": ["investors.sofi.com"],
            }
        },
        max_articles=0,
        max_evidence=1,
    )
    assert notes[0]["raw_published_date"] == "21 hours ago"
    assert notes[0]["published_date"] == "2026-07-19"
    assert notes[0]["date_reference_datetime"] == "2026-07-19T22:02:58+02:00"


def _snapshot() -> dict:
    return {
        "portfolio_name": "Portfolio",
        "report_date": "2026-07-19",
        "as_of": "2026-07-19T22:00:00+02:00",
        "base_currency": "EUR",
        "benchmark": "^GSPC",
        "summary": {"total_market_value_base": 1000.0},
        "data_quality": {},
        "holdings": [{"ticker": "SOFI", "weight": 1.0, "market_value_base": 1000.0}],
        "run_timeline": {
            "snapshot_completed_at": "2026-07-19T22:00:00+02:00",
            "news_search_completed_at": "2026-07-19T22:02:58+02:00",
            "report_rendered_at": "2026-07-19T22:04:00+02:00",
        },
        "data_cutoffs": {"equity": "2026-07-17", "benchmark": "2026-07-17"},
    }


def test_final_gate_rejection_reason_and_item_are_rendered():
    item = {
        "evidence_uid": "ev_sofi",
        "ticker": "SOFI",
        "title": "SoFi announces Q2 results date",
        "url": "https://investors.sofi.com/news/q2",
        "source_domain": "investors.sofi.com",
        "source_type": "official",
        "lane": "official",
        "raw_published_date": "21 hours ago",
        "published_date": "2026-07-19",
        "date_reference_datetime": "2026-07-19T22:02:58+02:00",
        "materiality_accepted": True,
        "summary_integrity_ok": True,
        "accept": True,
        "entity_role": "primary",
        "recency_tier": "fresh_event",
        "article_fetch_ok": False,
        "snippet_fallback_ok": False,
    }
    assert evidence_final_gate_reasons(item) == ["content_not_verified_or_snippet_too_weak"]
    diagnostics = evidence_stage_diagnostics([item])
    diagnostics.update({
        "status": "insufficient_coverage",
        "raw_results_count": 1,
        "filtered_results_count": 1,
        "accepted_evidence_count": 0,
        "latest_selected_event_date": "2026-07-19",
        "materiality_stats": {"accepted_count": 1, "rejected_count": 0, "rejected_reasons": {}},
        "rejected": {},
    })
    advice = {
        "report_mode": "quantitative_fallback",
        "observation_only": True,
        "portfolio_stance": "observe",
        "risk_level": "medium_high",
        "confidence": 0.4,
        "final_confidence": 0.0,
        "confidence_components": {},
        "executive_summary": ["量化观察。"],
        "key_risks": [],
        "actions": [],
        "portfolio_analysis": {},
        "watch_items": [],
        "data_limitations": [],
    }
    html = build_html(
        _snapshot(),
        {
            "portfolio_risk_score": 54,
            "portfolio_risk_level": "medium_high",
            "risk_score_confidence": 0.85,
            "risk_score_component_max": {},
            "relative_returns": {},
            "risk_contributions": [],
            "aggregates": {},
        },
        {"top_risk_tickers": ["SOFI"]},
        advice,
        [],
        research_diagnostics=diagnostics,
        report_quality={"publishable": True, "observation_only": True, "quality_score": 0.0},
        rejected_evidence=[item],
    )
    assert "最终 Evidence Gate 拒绝原因" in html
    assert "正文未验证且搜索摘要不足以作为证据" in html
    assert "最终 Evidence Gate 拒绝明细" in html
    assert "investors.sofi.com" in html
    assert "21 hours ago" in html
    assert "2026-07-19" in html
    assert "过滤标签计数（同一结果可能命中多个标签）" in html
