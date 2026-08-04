"""Offline parser tests for SerpAPI Google News."""

from __future__ import annotations

from daily_report.src.stock_daily_agent.search_provider_clients import (
    flatten_serpapi_news_results,
    run_serpapi_raw_search,
)


def test_flatten_serpapi_news_preserves_direct_stories_and_highlights_only() -> None:
    payload = {
        "news_results": [
            {"title": "Direct", "link": "https://news.example/direct", "position": 1},
            {
                "title": "Grouped",
                "highlight": {"title": "Highlight", "link": "https://news.example/highlight"},
                "stories": [
                    {"title": "Story", "link": "https://news.example/story"},
                    {"title": "Duplicate", "link": "https://news.example/direct"},
                ],
                "related_topics": [{"title": "Navigation", "link": "https://news.example/nav"}],
            },
        ]
    }

    items = flatten_serpapi_news_results(payload)
    assert [item["link"] for item in items] == [
        "https://news.example/direct", "https://news.example/highlight", "https://news.example/story",
    ]
    assert items[1]["parent_group_title"] == "Grouped"


def test_serpapi_prefers_iso_date_and_does_not_expose_key(monkeypatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "secret-serpapi-key")
    monkeypatch.setenv("SERPAPI_SLEEP_SECONDS", "0")

    def fake_get(*args, **kwargs):
        return {"news_results": [{
            "title": "MSFT upgrade", "link": "https://reuters.com/msft", "snippet": "Analyst upgrade",
            "iso_date": "2026-08-01T10:30:00Z", "date": "2 days ago", "source": {"name": "Reuters"},
        }]}

    items, calls, errors = run_serpapi_raw_search(
        ticker="MSFT", languages=["en-US"], queries={"en-US": [("MSFT upgrade", "analyst_ratings")]},
        report_date="2026-08-01", max_per_query=5, http_get=fake_get,
    )
    assert not errors
    assert items[0]["source_date"] == "2026-08-01T10:30:00Z"
    assert items[0]["source"] == "Reuters"
    assert calls[0]["request_query"].endswith("when:90d")

    def broken(*args, **kwargs):
        raise RuntimeError("invalid api_key=secret-serpapi-key")

    _, _, errors = run_serpapi_raw_search(
        ticker="MSFT", languages=["en-US"], queries={"en-US": [("MSFT upgrade", "analyst_ratings")]},
        report_date="2026-08-01", max_per_query=5, http_get=broken,
    )
    assert "secret-serpapi-key" not in " ".join(errors)
