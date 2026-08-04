"""Offline production-chain tests: sequence, quality gate and article cache."""

from __future__ import annotations

import json
from pathlib import Path

from daily_report.src.stock_daily_agent import article_fetcher, tools
from daily_report.src.stock_daily_agent.config import ProjectPaths, RunContext


def _ctx(tmp_path: Path) -> RunContext:
    ctx = RunContext(
        paths=ProjectPaths.from_root(tmp_path / "daily_report"), ticker="AAPL", run_dir=tmp_path / "run", report_date="2026-08-01",
    )
    ctx.run_dir.mkdir(parents=True)
    ctx.data_file.write_text(json.dumps({"INSTRUMENT_TYPE": "ETF"}), encoding="utf-8")
    return ctx


def _raw(provider: str, suffix: str) -> list[dict]:
    return [{
        "title": f"AAPL major event {suffix}", "facts": "AAPL revenue update", "url": f"https://reuters.com/{suffix}",
        "source_date": "2026-08-01", "focus": "major_events", "provider": provider, "engine": provider,
    }]


def _set_quality_env(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", "priority")
    monkeypatch.setenv("SEARCH_PROVIDER_PRIORITY", "serper,anspire,serpapi,dashscope,searxng")
    monkeypatch.setenv("SEARCH_MIN_FINAL_EVIDENCE", "1")
    monkeypatch.setenv("SEARCH_MIN_GRADE_AB", "0")
    monkeypatch.setenv("SEARCH_MIN_KNOWN_DATE_EVIDENCE", "0")
    monkeypatch.setenv("SEARCH_MIN_DIRECT_RELEVANCE_EQUITY", "0")
    monkeypatch.setenv("SEARCH_REQUIRED_FOCUS_COVERAGE", "major_events")
    monkeypatch.setenv("ARTICLE_FETCH_ENABLED", "false")


def test_priority_stops_after_serper_when_filtered_quality_is_sufficient(tmp_path, monkeypatch) -> None:
    _set_quality_env(monkeypatch)
    monkeypatch.setenv("SERPER_API_KEY", "x")
    monkeypatch.setenv("ANSPIRE_API_KEY", "x")
    monkeypatch.setenv("SERPAPI_API_KEY", "x")
    called: list[str] = []
    monkeypatch.setattr(tools, "_run_serper_raw_search", lambda *args: (called.append("serper") or _raw("serper", "s"), [], []))
    monkeypatch.setattr(tools, "_run_anspire_provider", lambda *args: (called.append("anspire") or _raw("anspire", "a"), [], []))
    monkeypatch.setattr(tools, "_run_serpapi_provider", lambda *args: (called.append("serpapi") or _raw("serpapi", "p"), [], []))
    ctx = _ctx(tmp_path)
    tools.set_context(ctx)

    result = json.loads(tools.PriorityMarketResearchTool().call({"ticker": "AAPL"}))
    report = json.loads(ctx.search_quality_report_file.read_text(encoding="utf-8"))
    assert called == ["serper"]
    assert result["selected_stop_provider"] == "serper"
    assert [stage["provider"] for stage in report["stages"]] == ["serper", "anspire", "serpapi", "dashscope", "searxng"]
    assert report["stages"][1]["skip_reason"] == "quality_satisfied"
    assert report["stages"][0]["admitted_count"] == 1


def test_priority_accumulates_and_skips_missing_key_without_failing(tmp_path, monkeypatch) -> None:
    _set_quality_env(monkeypatch)
    monkeypatch.setenv("SERPER_API_KEY", "x")
    monkeypatch.delenv("ANSPIRE_API_KEY", raising=False)
    monkeypatch.setenv("SERPAPI_API_KEY", "x")
    called: list[str] = []
    monkeypatch.setattr(tools, "_run_serper_raw_search", lambda *args: (called.append("serper") or [], [], []))
    monkeypatch.setattr(tools, "_run_serpapi_provider", lambda *args: (called.append("serpapi") or _raw("serpapi", "p"), [], []))
    ctx = _ctx(tmp_path)
    tools.set_context(ctx)

    result = json.loads(tools.PriorityMarketResearchTool().call({"ticker": "AAPL"}))
    report = json.loads(ctx.search_quality_report_file.read_text(encoding="utf-8"))
    assert called == ["serper", "serpapi"]
    assert result["selected_stop_provider"] == "serpapi"
    assert result["items"][0]["provider_sources"] == ["serpapi"]
    assert report["stages"][1]["skip_reason"] == "provider_missing_key"


def test_stale_serper_result_is_rejected_and_anspire_fallback_is_used(tmp_path, monkeypatch) -> None:
    _set_quality_env(monkeypatch)
    monkeypatch.setenv("SERPER_API_KEY", "x")
    monkeypatch.setenv("ANSPIRE_API_KEY", "x")
    called: list[str] = []
    stale = _raw("serper", "stale")
    stale[0]["source_date"] = "2020-01-01"
    monkeypatch.setattr(tools, "_run_serper_raw_search", lambda *args: (called.append("serper") or stale, [], []))
    monkeypatch.setattr(tools, "_run_anspire_provider", lambda *args: (called.append("anspire") or _raw("anspire", "a"), [], []))
    ctx = _ctx(tmp_path)
    tools.set_context(ctx)

    result = json.loads(tools.PriorityMarketResearchTool().call({"ticker": "AAPL"}))
    report = json.loads(ctx.search_quality_report_file.read_text(encoding="utf-8"))
    assert called == ["serper", "anspire"]
    assert result["selected_stop_provider"] == "anspire"
    assert report["stages"][0]["admitted_count"] == 0
    assert report["stages"][0]["rejection_counts"]["stale_date"] == 1


def test_priority_uses_serpapi_after_serper_and_anspire_are_insufficient(tmp_path, monkeypatch) -> None:
    _set_quality_env(monkeypatch)
    monkeypatch.setenv("SERPER_API_KEY", "x")
    monkeypatch.setenv("ANSPIRE_API_KEY", "x")
    monkeypatch.setenv("SERPAPI_API_KEY", "x")
    called: list[str] = []
    monkeypatch.setattr(tools, "_run_serper_raw_search", lambda *args: (called.append("serper") or [], [], []))
    monkeypatch.setattr(tools, "_run_anspire_provider", lambda *args: (called.append("anspire") or [], [], []))
    monkeypatch.setattr(tools, "_run_serpapi_provider", lambda *args: (called.append("serpapi") or _raw("serpapi", "p"), [], []))
    ctx = _ctx(tmp_path)
    tools.set_context(ctx)

    result = json.loads(tools.PriorityMarketResearchTool().call({"ticker": "AAPL"}))
    assert called == ["serper", "anspire", "serpapi"]
    assert result["selected_stop_provider"] == "serpapi"


def test_dashscope_real_source_can_complete_quality_without_calling_searxng(tmp_path, monkeypatch) -> None:
    _set_quality_env(monkeypatch)
    monkeypatch.setenv("SEARCH_MIN_FINAL_EVIDENCE", "4")
    monkeypatch.setenv("SERPER_API_KEY", "x")
    monkeypatch.setenv("ANSPIRE_API_KEY", "x")
    monkeypatch.setenv("SERPAPI_API_KEY", "x")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    called: list[str] = []
    monkeypatch.setattr(tools, "_run_serper_raw_search", lambda *args: (called.append("serper") or _raw("serper", "s"), [], []))
    monkeypatch.setattr(tools, "_run_anspire_provider", lambda *args: (called.append("anspire") or _raw("anspire", "a"), [], []))
    monkeypatch.setattr(tools, "_run_serpapi_provider", lambda *args: (called.append("serpapi") or _raw("serpapi", "p"), [], []))
    ctx = _ctx(tmp_path)

    def fake_dashscope(self, params):
        called.append("dashscope")
        ctx.dashscope_sources_file.write_text(json.dumps({"items": [
            {"title": "AAPL sourced filing", "facts": "AAPL guidance", "url": "https://sec.gov/aapl", "source": "SEC", "source_date": "2026-08-01"},
        ], "candidates": [{"title": "not a source", "url": ""}]}), encoding="utf-8")
        return json.dumps({"ok": True})

    monkeypatch.setattr(tools.DashScopeMarketResearchTool, "call", fake_dashscope)
    tools.set_context(ctx)
    result = json.loads(tools.PriorityMarketResearchTool().call({"ticker": "AAPL"}))
    report = json.loads(ctx.search_quality_report_file.read_text(encoding="utf-8"))

    assert called == ["serper", "anspire", "serpapi", "dashscope"]
    assert result["selected_stop_provider"] == "dashscope"
    assert {item["title"] for item in result["items"]} == {
        "AAPL major event s", "AAPL major event a", "AAPL major event p", "AAPL sourced filing",
    }
    assert report["stages"][-1]["provider"] == "searxng"
    assert report["stages"][-1]["skip_reason"] == "quality_satisfied"


def test_priority_reaches_searxng_only_when_all_previous_stages_are_insufficient(tmp_path, monkeypatch) -> None:
    _set_quality_env(monkeypatch)
    monkeypatch.setenv("SERPER_API_KEY", "x")
    monkeypatch.setenv("ANSPIRE_API_KEY", "x")
    monkeypatch.setenv("SERPAPI_API_KEY", "x")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    monkeypatch.setenv("SEARXNG_URL", "https://search.example")
    called: list[str] = []
    monkeypatch.setattr(tools, "_run_serper_raw_search", lambda *args: (called.append("serper") or [], [], []))
    monkeypatch.setattr(tools, "_run_anspire_provider", lambda *args: (called.append("anspire") or [], [], []))
    monkeypatch.setattr(tools, "_run_serpapi_provider", lambda *args: (called.append("serpapi") or [], [], []))
    monkeypatch.setattr(tools, "_run_searxng_raw_search", lambda *args: (called.append("searxng") or _raw("searxng", "sx"), [], []))
    ctx = _ctx(tmp_path)

    def fake_dashscope(self, params):
        called.append("dashscope")
        ctx.dashscope_sources_file.write_text(json.dumps({"items": [], "candidates": [{"title": "model only"}]}), encoding="utf-8")
        return json.dumps({"ok": True})

    monkeypatch.setattr(tools.DashScopeMarketResearchTool, "call", fake_dashscope)
    tools.set_context(ctx)
    result = json.loads(tools.PriorityMarketResearchTool().call({"ticker": "AAPL"}))
    assert called == ["serper", "anspire", "serpapi", "dashscope", "searxng"]
    assert result["selected_stop_provider"] == "searxng"


def test_article_enrichment_cache_fetches_same_canonical_url_once(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch(url: str, **kwargs) -> dict:
        calls.append(url)
        return {"url": url, "ok": True, "article_text_quality_ok": True, "text": "Revenue 123 " * 100, "title": "AAPL update"}

    monkeypatch.setattr(article_fetcher, "_fetch_article_text", fake_fetch)
    cache: dict[str, dict] = {}
    item = {"title": "AAPL update", "url": "https://reuters.com/aapl?utm_source=x", "facts": "AAPL revenue", "source_quality_score": 92}
    _, first = article_fetcher._enrich_evidence_with_articles([item], max_urls=2, max_chars=3500, timeout=1, article_cache=cache)
    _, second = article_fetcher._enrich_evidence_with_articles([item], max_urls=2, max_chars=3500, timeout=1, article_cache=cache)
    assert calls == ["https://reuters.com/aapl?utm_source=x"]
    assert len(first) == 1
    assert not second
