"""Offline coverage for Model Studio Responses API web-search adaptation."""

from __future__ import annotations

import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "daily_report" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from stock_daily_agent import tools
from stock_daily_agent.config import ProjectPaths, RunContext


def _response() -> dict:
    return {
        "id": "resp_123",
        "output_text": '{"items":[{"title":"Quarterly results","url":"https://example.com/earnings","facts":"Results released today.","source":"Example","source_date":"2026-11-09","relevance":"Earnings update","sentiment_hint":"BULL"}]}',
        "output": [
            {
                "type": "web_search_call",
                "action": {"sources": [
                    {"url": "https://example.com/earnings", "title": "Quarterly results", "snippet": "Results released today."},
                    {"url": "https://example.com/earnings?utm_source=test", "title": "Duplicate"},
                ]},
            },
            {"type": "message", "content": [{"type": "output_text", "text": "fallback text"}]},
        ],
        "usage": {"x_tools": {"web_search": {"count": 1}}},
    }


def test_responses_parser_preserves_deduplicated_search_sources() -> None:
    text, sources, meta = tools._dashscope_responses_content_and_sources(_response())

    assert '"items"' in text
    assert len(sources) == 1
    assert sources[0]["url"] == "https://example.com/earnings"
    assert sources[0]["title"] == "Quarterly results"
    assert meta["protocol"] == "responses_api"


def test_v41_research_writes_existing_ds_source_artifact(tmp_path, monkeypatch) -> None:
    ctx = RunContext(
        paths=ProjectPaths.from_root(tmp_path / "daily_report"),
        ticker="AAPL",
        run_dir=tmp_path / "run",
        report_date="2026-11-09",
    )
    ctx.run_dir.mkdir(parents=True)
    tools.set_context(ctx)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_RESEARCH_MODEL", "deepseek-v4.1-flash-1017")
    monkeypatch.setattr(tools, "_call_dashscope_responses_search", lambda **_kwargs: _response())

    result = json.loads(tools.DashScopeMarketResearchTool().call({"ticker": "AAPL", "min_items": 1}))
    saved = json.loads(ctx.dashscope_sources_file.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["sources_count"] == 1
    assert result["items"][0]["evidence_id"] == "DS001"
    assert saved["items"][0]["evidence_id"] == "DS001"
    assert saved["meta"]["protocol"] == "responses_api"
