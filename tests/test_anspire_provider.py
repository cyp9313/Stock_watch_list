"""Offline parser and safety tests for the Anspire Open adapter."""

from __future__ import annotations

from daily_report.src.stock_daily_agent.search_provider_clients import run_anspire_raw_search


def test_anspire_maps_results_limits_queries_and_keeps_credentials_out_of_errors(monkeypatch) -> None:
    monkeypatch.setenv("ANSPIRE_API_KEY", "secret-anspire-key")
    monkeypatch.setenv("ANSPIRE_SLEEP_SECONDS", "0")
    observed: list[dict] = []

    def fake_get(url: str, *, params: dict, headers: dict, timeout: float) -> dict:
        observed.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return {
            "results": [
                {"title": "AAPL earnings update", "content": "Revenue and guidance update", "url": "https://news.example/aapl", "date": "2026-08-01", "score": 0.91},
                {"title": "No URL", "content": "must be rejected", "date": "2026-08-01"},
                {"title": "Epoch", "content": "placeholder date", "url": "https://news.example/epoch", "date": "1970-01-01"},
            ]
        }

    long_query = "AAPL " + "analyst rating target price upgrade downgrade " * 5
    items, calls, errors = run_anspire_raw_search(
        ticker="AAPL",
        languages=["en-US"],
        queries={"en-US": [(long_query, "analyst_ratings")]},
        report_date="2026-08-01",
        max_per_query=10,
        http_get=fake_get,
    )

    assert not errors
    assert len(items) == 2
    assert items[0]["provider"] == "anspire"
    assert items[0]["provider_score"] == 0.91
    assert "source_quality_score" not in items[0]
    assert items[1]["source_date"] == "unknown"
    assert len(observed[0]["params"]["query"]) <= 64
    assert observed[0]["params"]["region_mode"] == 1
    assert observed[0]["params"]["FromTime"] == "2026-05-03 00:00:00"
    assert observed[0]["params"]["ToTime"] == "2026-08-01 23:59:59"
    assert observed[0]["headers"]["Authorization"] == "Bearer secret-anspire-key"
    assert calls[0]["request_query"] == observed[0]["params"]["query"]


def test_anspire_uses_china_region_and_redacts_key_from_errors(monkeypatch) -> None:
    monkeypatch.setenv("ANSPIRE_API_KEY", "do-not-leak")
    monkeypatch.setenv("ANSPIRE_SLEEP_SECONDS", "0")

    def broken(*args, **kwargs):
        raise RuntimeError("upstream rejected Bearer do-not-leak")

    items, _, errors = run_anspire_raw_search(
        ticker="600519.SS",
        languages=["zh-CN"],
        queries={"zh-CN": [("贵州茅台 财报", "earnings")]},
        report_date="2026-08-01",
        max_per_query=3,
        http_get=broken,
    )

    assert not items
    assert errors and "do-not-leak" not in " ".join(errors)

