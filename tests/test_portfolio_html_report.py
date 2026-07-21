# -*- coding: utf-8 -*-
"""Portfolio 中文报告 HTML 渲染测试（修改计划 16/17）。"""
from __future__ import annotations

from daily_report.scripts.build_portfolio_report import build_html
from daily_report.report_components import render_action_detail


def _snapshot():
    return {
        "portfolio_name": "<script>alert(1)</script>",
        "base_currency": "EUR",
        "benchmark": "^GSPC",
        "holdings": [{
            "ticker": "AAA", "group": "<b>AI</b>", "weight": 1.0,
            "market_value_base": 100, "return_1d": 1.0, "rsi": 50.0,
        }],
        "summary": {"total_market_value_base": 100},
        "data_quality": {},
    }


def test_portfolio_html_escapes_untrusted_text():
    html = build_html(
        _snapshot(),
        {"top1_weight": 1.0, "risk_contributions": [{"ticker": "AAA", "risk_contribution": 1.0}]},
        {"items": [{"ticker": "AAA", "risk_priority_score": 1.0}]},
        {"actions": [{"ticker": "AAA", "action": "hold"}], "summary": "<img src=x onerror=alert(1)>",
         "report_mode": "ai"},
        [{"evidence_id": "E1", "title": "<svg onload=alert(1)>", "url": "https://example.com"}],
    )
    assert "<script>alert(1)</script>" not in html
    assert "<svg onload=alert(1)>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # 修改计划：报告改为简体中文，含中文免责声明
    assert "本报告仅供研究参考，不构成投资建议。" in html
    assert 'lang="zh-CN"' in html
    assert "AI 核心结论" in html


def test_portfolio_html_fallback_banner_when_quantitative_fallback():
    html = build_html(
        _snapshot(),
        {"top1_weight": 1.0, "risk_contributions": [{"ticker": "AAA", "risk_contribution": 1.0}]},
        {"items": [{"ticker": "AAA", "risk_priority_score": 1.0}]},
        {"actions": [{"ticker": "AAA", "action": "watch"}], "report_mode": "quantitative_fallback"},
        [],
    )
    # 量化降级报告必须被清晰标注，且不得伪装成 AI 分析
    assert "量化降级报告" in html
    assert "本报告仅供研究参考，不构成投资建议。" in html


def test_portfolio_html_ai_mode_has_no_fallback_banner():
    html = build_html(
        _snapshot(),
        {"top1_weight": 0.5, "risk_contributions": [{"ticker": "AAA", "risk_contribution": 0.5}]},
        {"items": [{"ticker": "AAA", "risk_priority_score": 0.5}]},
        {
            "actions": [{"ticker": "AAA", "action": "hold"}], "report_mode": "ai",
            "executive_summary": ["市场情绪谨慎偏多。"],
            "key_risks": [{"risk_id": "R001", "title": "集中度风险", "severity": "high"}],
        },
        [{"evidence_id": "E1", "title": "Apple 财报", "url": "https://example.com/a"}],
    )
    assert "量化降级报告" not in html
    assert "Apple 财报" in html
    assert "集中度风险" in html


def test_portfolio_news_links_use_the_soft_high_contrast_color():
    html = build_html(
        _snapshot(),
        {"top1_weight": 0.5, "risk_contributions": [{"ticker": "AAA", "risk_contribution": 0.5}]},
        {"items": [{"ticker": "AAA", "risk_priority_score": 0.5}]},
        {"actions": [{"ticker": "AAA", "action": "hold"}], "report_mode": "ai"},
        [{"evidence_id": "E1", "title": "News title", "url": "https://example.com/a"}],
    )

    assert ".source-card .sc-title a { color: #a8d5ba;" in html


def test_observation_action_renders_metric_evidence_for_python_311_compatible_template():
    html = render_action_detail(
        {
            "ticker": "AAA",
            "action": "watch",
            "metric_evidence": [{"ticker": "AAA", "metric": "rsi"}],
        },
        ticker_metrics={"rsi": 50.0},
        observation_only=True,
    )

    assert "指标证据（确定性数据）" in html
    assert "AAA · rsi：50" in html


def test_action_detail_renders_thresholds_and_metric_evidence():
    html = render_action_detail(
        {
            "ticker": "AAA",
            "action": "hold",
            "thresholds": [{"metric": "weight", "value": 0.2, "basis": "evidence"}],
            "metric_evidence": [{"ticker": "AAA", "metric": "rsi"}],
        },
        ticker_metrics={"rsi": 50.0},
    )

    assert "关键阈值与依据" in html
    assert "指标证据（确定性数据）" in html
