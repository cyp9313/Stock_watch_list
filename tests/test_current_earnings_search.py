from daily_report.src.stock_daily_agent import tools


def test_equity_search_prioritizes_current_earnings_season_and_official_site():
    queries = tools._build_market_queries(
        "VST",
        {
            "LONG_NAME": "Vistra Corp.",
            "INSTRUMENT_TYPE": "EQUITY",
            "WEBSITE": "https://investor.vistracorp.com/",
        },
        "en-US",
        report_date="2026-08-08",
    )
    assert queries[0] == ("Vistra Corp. VST Q2 2026 earnings results actual EPS revenue guidance", "earnings_current")
    assert queries[1] == ("site:investor.vistracorp.com Vistra Corp. VST Q2 2026 earnings results", "earnings_current")
    assert ("Vistra Corp. VST latest earnings revenue EPS guidance", "earnings") in queries


def test_current_earnings_evidence_satisfies_ordinary_earnings_coverage():
    quality = tools.evaluate_search_quality(
        [{"focus": "earnings_current", "evidence_grade": "A", "source_date": "2026-08-07", "target_relevance_category": "direct_company_news"}],
        required_focus=["earnings"],
    )
    assert quality["missing_focus"] == []


def test_current_earnings_evidence_is_ranked_before_generic_earnings():
    ranked = tools._rerank_evidence([
        {"focus": "earnings", "url": "https://example.com/old", "source_date": "2026-05-07", "facts": "old generic result"},
        {"focus": "earnings_current", "url": "https://example.com/current", "source_date": "2026-08-07", "facts": "current quarterly result"},
    ])
    assert ranked[0]["focus"] == "earnings_current"
