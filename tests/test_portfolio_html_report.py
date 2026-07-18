from __future__ import annotations

from daily_report.scripts.build_portfolio_report import build_html


def test_portfolio_html_escapes_untrusted_text():
    html = build_html(
        {
            "portfolio_name": "<script>alert(1)</script>",
            "base_currency": "EUR",
            "benchmark": "^GSPC",
            "holdings": [{"ticker": "AAA", "group": "<b>AI</b>", "weight": 1.0, "market_value_base": 100}],
            "summary": {"total_market_value_base": 100},
            "data_quality": {},
        },
        {"top1_weight": 1.0, "risk_contributions": [{"ticker": "AAA", "risk_contribution": 1.0}]},
        {"items": [{"ticker": "AAA", "risk_priority_score": 1.0}]},
        {"actions": [{"ticker": "AAA", "action": "hold"}], "summary": "<img src=x onerror=alert(1)>"},
        [{"evidence_id": "E1", "title": "<svg onload=alert(1)>", "url": "https://example.com"}],
    )

    assert "<script>alert(1)</script>" not in html
    assert "<svg onload=alert(1)>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "This report is for research purposes only" in html
