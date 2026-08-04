"""Regression coverage for local news date, relevance and admission gates."""

from __future__ import annotations

from datetime import datetime, timezone

from daily_report.src.stock_daily_agent.search_provider_clients import (
    canonicalize_url,
    normalize_news_date,
    parse_provider_priority,
    provider_priority_warnings,
    prepare_search_candidates,
)


def test_normalize_news_date_supports_provider_formats() -> None:
    report_date = "2026-08-01T12:00:00Z"
    assert normalize_news_date("2026-08-01", report_date=report_date)
    assert normalize_news_date("Fri, 31 Jul 2026 12:00:00 GMT", report_date=report_date)
    assert normalize_news_date(1785585600000, report_date=report_date)
    assert normalize_news_date("2 hours ago", report_date=report_date) == datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    assert normalize_news_date("3 days ago", report_date=report_date) == datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    assert normalize_news_date("昨天", report_date=report_date) == datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    assert normalize_news_date("1970-01-01", report_date=report_date) is None


def test_provider_priority_is_stable_and_ignores_unknown_values() -> None:
    raw = " Serper, anspire, SERPER,unknown,serpapi "
    assert parse_provider_priority(raw) == ["serper", "anspire", "serpapi"]
    assert provider_priority_warnings(raw) == ["unknown_provider_ignored:unknown"]


def test_candidate_pipeline_filters_stale_future_junk_and_irrelevant_but_keeps_app_business_news(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_UNKNOWN_DATE_LIMIT", "4")
    raw = [
        {"title": "APP revenue and downloads growth", "facts": "APP reports strong revenue", "url": "https://reuters.com/app", "source_date": "2026-08-01", "focus": "major_events", "provider": "serper"},
        {"title": "Old APP event", "facts": "old", "url": "https://reuters.com/old", "source_date": "2020-01-01", "focus": "major_events", "provider": "serper"},
        {"title": "Future APP event", "facts": "future", "url": "https://reuters.com/future", "source_date": "2026-08-04", "focus": "major_events", "provider": "serper"},
        {"title": "APP free APK download", "facts": "install now", "url": "https://download.example/app.apk", "source_date": "2026-08-01", "focus": "major_events", "provider": "serper"},
        {"title": "Unrelated local weather", "facts": "sunny", "url": "https://weather.example/today", "source_date": "2026-08-01", "focus": "major_events", "provider": "serper"},
    ]

    accepted, rejected = prepare_search_candidates(
        raw, ticker="APP", data={"INSTRUMENT_TYPE": "EQUITY", "SHORT_NAME": "AppLovin"},
        report_date="2026-08-01", max_items=20,
    )
    assert [item["url"] for item in accepted] == ["https://reuters.com/app"]
    assert {reason for item in rejected for reason in item.get("rejection_reasons", [])} >= {
        "stale_date", "future_date", "non_article_asset", "irrelevant_target",
    }


def test_canonical_dedupe_merges_provider_sources() -> None:
    assert canonicalize_url("https://www.example.com/news/?utm_source=x&ref=y&id=4#section") == "https://example.com/news?id=4"
    raw = [
        {"title": "AAPL earnings", "facts": "AAPL revenue", "url": "https://www.reuters.com/aapl?utm_source=x", "source_date": "2026-08-01", "focus": "earnings", "provider": "serper"},
        {"title": "AAPL earnings", "facts": "AAPL revenue", "url": "https://reuters.com/aapl", "source_date": "2026-08-01", "focus": "earnings", "provider": "anspire"},
    ]
    accepted, _ = prepare_search_candidates(raw, ticker="AAPL", data={"INSTRUMENT_TYPE": "EQUITY"}, report_date="2026-08-01", max_items=20)
    assert len(accepted) == 1
    assert accepted[0]["provider_sources"] == ["serper", "anspire"]
