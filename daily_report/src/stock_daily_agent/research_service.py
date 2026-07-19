from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import requests


class ResearchService:
    """Small structured search adapter shared by portfolio reports.

    It intentionally returns evidence records, not prose conclusions.  Article
    body fetching remains optional and should use the existing SSRF-protected
    article fetcher when enabled by callers.

    第六轮（修改计划第 14.2 节）：支持 Serper News vertical 与普通 Search
    vertical 双通道。通过 ``use_news_vertical`` 参数控制；环境变量
    ``PORTFOLIO_SERPER_TYPES`` 与 ``PORTFOLIO_SERPER_NEWS_FIRST`` 决定默认行为。
    """

    def search(
        self,
        queries: list[str],
        *,
        provider: str = "auto",
        max_results: int = 5,
        recency_days: int | None = None,
        use_news_vertical: bool | None = None,
    ) -> dict[str, Any]:
        requested = (provider or "auto").lower()
        configured = []
        if os.environ.get("SERPER_API_KEY"):
            configured.append("serper")
        if os.environ.get("SEARXNG_URL"):
            configured.append("searxng")
        diagnostics: dict[str, Any] = {
            "status": "success",
            "provider_requested": requested,
            "provider_used": None,
            "queries_count": len(queries),
            "raw_results_count": 0,
            "errors": [],
            "verticals_used": [],
        }
        if not configured:
            diagnostics["status"] = "not_configured"
            diagnostics["errors"] = [
                "SERPER_API_KEY not configured",
                "SEARXNG_URL not configured",
            ]
            return {"results": [], "diagnostics": diagnostics}
        evidence: list[dict[str, Any]] = []
        verticals_used: set[str] = set()
        for query in queries:
            try:
                results, vertical = self._search_one(
                    query, provider=provider, max_results=max_results,
                    recency_days=recency_days, use_news_vertical=use_news_vertical,
                )
                evidence.extend(results)
                if vertical:
                    verticals_used.add(vertical)
            except requests.RequestException as exc:
                diagnostics["errors"].append(f"{type(exc).__name__}: {exc}")
        # 第七轮第 3 节：原始搜索结果不得分配最终 evidence_id（子流程只产生候选输入）。
        # 最终编号由研究管线的收口点在 accepted 证据上统一分配。
        diagnostics["raw_results_count"] = len(evidence)
        diagnostics["provider_used"] = evidence[0].get("provider") if evidence else (configured[0] if configured else None)
        diagnostics["verticals_used"] = sorted(verticals_used)
        if not evidence:
            diagnostics["status"] = "provider_error" if diagnostics["errors"] else "no_raw_results"
        return {"results": evidence, "diagnostics": diagnostics}

    def _search_one(
        self, query: str, *, provider: str, max_results: int,
        recency_days: int | None = None,
        use_news_vertical: bool | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """返回 (results, vertical_used)。

        vertical_used ∈ {"news", "search", ""}，用于诊断。
        """
        provider = (provider or "auto").lower()
        # 决定是否使用 news vertical
        serper_types_env = [
            s.strip().lower() for s in os.environ.get("PORTFOLIO_SERPER_TYPES", "news,search").split(",")
            if s.strip()
        ]
        news_first = os.environ.get("PORTFOLIO_SERPER_NEWS_FIRST", "true").strip().lower() in {"1", "true", "yes"}

        # 显式参数优先；否则根据环境变量默认
        if use_news_vertical is None:
            use_news_vertical = news_first and "news" in serper_types_env

        if provider in {"auto", "priority", "serper", "both"} and os.environ.get("SERPER_API_KEY"):
            # 先尝试 news vertical（如果启用）
            if use_news_vertical and "news" in serper_types_env:
                news_results = self._serper_news_search(query, max_results=max_results, recency_days=recency_days)
                if news_results:
                    return news_results, "news"
                # news 无结果时 fallback 到 search（仅当 search 也在允许列表）
                if "search" in serper_types_env:
                    search_results = self._serper_search(query, max_results=max_results, recency_days=recency_days)
                    return search_results, "search"
                return [], "news"
            # 仅 search vertical
            if "search" in serper_types_env or provider == "serper":
                results = self._serper_search(query, max_results=max_results, recency_days=recency_days)
                return results, "search"
            return [], ""
        if provider in {"auto", "priority", "searxng", "both"} and os.environ.get("SEARXNG_URL"):
            results = self._searxng_search(query, max_results=max_results, recency_days=recency_days)
            return results, "searxng"
        return [], ""

    def _serper_search(
        self, query: str, *, max_results: int, recency_days: int | None = None,
    ) -> list[dict[str, Any]]:
        endpoint = os.environ.get("SERPER_ENDPOINT", "https://google.serper.dev/search")
        headers = {"X-API-KEY": os.environ["SERPER_API_KEY"], "Content-Type": "application/json"}
        request_payload: dict[str, Any] = {"q": query, "num": max_results}
        if recency_days and recency_days > 0:
            start = date.today() - timedelta(days=int(recency_days))
            # Serper forwards the q expression to Google.  The explicit date operator is
            # more portable than relying on an undocumented provider-specific tbs value.
            request_payload["q"] = f"{query} after:{start.isoformat()}"
        response = requests.post(endpoint, headers=headers, json=request_payload, timeout=15)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("organic") or []
        return [self._normalize_result(item, query, "serper", "search") for item in items[:max_results]]

    def _serper_news_search(
        self, query: str, *, max_results: int, recency_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """调用 Serper News vertical（https://google.serper.dev/news）。

        News vertical 返回结构含 ``news`` 数组（而非 ``organic``），每条含
        title/link/snippet/source/date。
        """
        base = os.environ.get("SERPER_API_BASE", "https://google.serper.dev")
        endpoint = f"{base.rstrip('/')}/news"
        headers = {"X-API-KEY": os.environ["SERPER_API_KEY"], "Content-Type": "application/json"}
        request_payload: dict[str, Any] = {"q": query, "num": max_results}
        if recency_days and recency_days > 0:
            start = date.today() - timedelta(days=int(recency_days))
            request_payload["q"] = f"{query} after:{start.isoformat()}"
        # Serper News 支持 gl/hl 参数
        gl = os.environ.get("SERPER_GL", "us")
        hl = os.environ.get("SERPER_HL", "en")
        if gl:
            request_payload["gl"] = gl
        if hl:
            request_payload["hl"] = hl
        response = requests.post(endpoint, headers=headers, json=request_payload, timeout=15)
        response.raise_for_status()
        payload = response.json()
        # Serper News 返回 ``news`` 数组；部分响应可能也含 ``organic``
        items = payload.get("news") or payload.get("organic") or []
        return [self._normalize_result(item, query, "serper", "news") for item in items[:max_results]]

    def _searxng_search(
        self, query: str, *, max_results: int, recency_days: int | None = None,
    ) -> list[dict[str, Any]]:
        base = os.environ["SEARXNG_URL"].rstrip("/")
        params = {"q": query, "format": "json", "language": os.environ.get("SEARXNG_LANGUAGE", "auto")}
        if recency_days and recency_days > 0:
            params["time_range"] = "month" if recency_days <= 45 else "year"
            params["q"] = f"{query} after:{(date.today() - timedelta(days=int(recency_days))).isoformat()}"
        response = requests.get(
            f"{base}/search",
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("results") or []
        return [self._normalize_result(item, query, "searxng", "search") for item in items[:max_results]]

    @staticmethod
    def _normalize_result(item: dict[str, Any], query: str, provider: str, vertical: str) -> dict[str, Any]:
        url = item.get("link") or item.get("url") or ""
        source = item.get("source") or item.get("sitename")
        if not source and url:
            source = urlparse(str(url)).netloc.lower().removeprefix("www.")
        return {
            "provider": provider,
            "vertical": vertical,
            "query": query,
            "title": item.get("title") or item.get("name") or "Untitled result",
            "url": url,
            "source": source or provider,
            "published_date": item.get("date") or item.get("publishedDate") or "",
            "summary": item.get("snippet") or item.get("content") or "",
        }
