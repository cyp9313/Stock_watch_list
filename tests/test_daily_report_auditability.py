"""Regression tests for daily-report provenance and field normalization."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "daily_report" / "scripts"))

from report_data_normalization import normalize_dividend_yield_pct
from test_p2_report_quality import _render_report, _report_data


def test_dividend_yield_uses_cash_dividend_to_resolve_provider_unit_ambiguity() -> None:
    """QQQ-like 0.41 provider value must not become a 41% yield."""
    value, source = normalize_dividend_yield_pct(0.41, 2.83, 690.22)

    assert value == 2.83 / 690.22 * 100
    assert source == "annual_dividend_rate/current_close"
    assert value < 1


def test_dividend_yield_rejects_uncorroborated_extreme_value() -> None:
    value, source = normalize_dividend_yield_pct(0.41, None, 690.22)

    assert value == 0
    assert source == "rejected_outlier"


def test_structured_evidence_renders_safe_link_id_and_stale_context() -> None:
    html = _render_report(
        _report_data(INSTRUMENT_TYPE="ETF", BETA=0, DIV_YIELD=0.41,
                     DIV_YIELD_SOURCE="annual_dividend_rate/current_close",
                     FUNDAMENTAL_SOURCES={"trailing_pe": "stockanalysis"}),
        report_date="2026-07-23",
        notes="[BULL] legacy fallback should not be rendered when evidence is available\n",
        evidence={
            "items": [{
                "tag": "BULL",
                "title": "ETF flow update",
                "fact": "Net inflows were reported.",
                "logic": "Flows can support short-term demand.",
                "investment_meaning": "Monitor follow-through.",
                "source": "Example Research",
                "source_date": "2026-07-10",
                "evidence_id": "E-101",
                "url": "https://example.com/research/qqq",
            }, {
                "tag": "MIX",
                "title": "Unsafe link is not rendered",
                "fact": "This is only a security regression fixture.",
                "logic": "Links must use HTTP(S).",
                "investment_meaning": "Do not make unsafe links clickable.",
                "source": "Fixture",
                "source_date": "2026-07-23",
                "evidence_id": "E-102",
                "url": "javascript:alert(1)",
            }]
        },
    )

    assert 'href="https://example.com/research/qqq"' in html
    assert "E-101" in html
    assert "背景资料 · 13 天前（2026-07-10）" in html
    assert "未附可核验链接" in html
    assert "javascript:alert" not in html
    assert "综合技术信号" in html
    assert "未舍入子项分数" in html
    assert "Beta（市场敏感度）" in html and "未获取；不据此判断波动" in html


def test_report_explains_dividend_yield_definition_and_evidence_scoring_freshness() -> None:
    data = _report_data(INSTRUMENT_TYPE="ETF", DIV_YIELD=0.26,
                        DIV_YIELD_SOURCE="annual_dividend_rate/current_close")
    data["final_rating"]["inputs"] = {
        "evidence_freshness": {"recent": 2, "aging": 1, "background": 3, "unknown": 0},
    }

    html = _render_report(data)

    assert "过去十二个月现金分配收益率（估算）" in html
    assert "不等同于30日SEC收益率" in html
    assert "30天以上 3 条仅作背景、不参与消息和风险评分" in html
