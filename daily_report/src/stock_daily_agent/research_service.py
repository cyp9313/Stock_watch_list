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
    """

    def search(
        self,
        queries: list[str],
        *,
        provider: str = "auto",
        max_results: int = 5,
        recency_days: int | None = None,
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
        }
        if not configured:
            diagnostics["status"] = "not_configured"
            diagnostics["errors"] = [
                "SERPER_API_KEY not configured",
                "SEARXNG_URL not configured",
            ]
            return {"results": [], "diagnostics": diagnostics}
        evidence: list[dict[str, Any]] = []
        for query in queries:
            try:
                results = self._search_one(
                    query, provider=provider, max_results=max_results, recency_days=recency_days,
                )
                evidence.extend(results)
            except requests.RequestException as exc:
                diagnostics["errors"].append(f"{type(exc).__name__}: {exc}")
        for index, item in enumerate(evidence, start=1):
            item.setdefault("evidence_id", f"E{index:03d}")
        diagnostics["raw_results_count"] = len(evidence)
        diagnostics["provider_used"] = evidence[0].get("provider") if evidence else (configured[0] if configured else None)
        if not evidence:
            diagnostics["status"] = "provider_error" if diagnostics["errors"] else "no_raw_results"
        return {"results": evidence, "diagnostics": diagnostics}

    def _search_one(
        self, query: str, *, provider: str, max_results: int, recency_days: int | None = None,
    ) -> list[dict[str, Any]]:
        provider = (provider or "auto").lower()
        if provider in {"auto", "priority", "serper", "both"} and os.environ.get("SERPER_API_KEY"):
            results = self._serper_search(query, max_results=max_results, recency_days=recency_days)
            if results or provider == "serper":
                return results
        if provider in {"auto", "priority", "searxng", "both"} and os.environ.get("SEARXNG_URL"):
            return self._searxng_search(query, max_results=max_results, recency_days=recency_days)
        return []

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
        return [self._normalize_result(item, query, "serper") for item in items[:max_results]]

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
        return [self._normalize_result(item, query, "searxng") for item in items[:max_results]]

    @staticmethod
    def _normalize_result(item: dict[str, Any], query: str, provider: str) -> dict[str, Any]:
        url = item.get("link") or item.get("url") or ""
        source = item.get("source") or item.get("sitename")
        if not source and url:
            source = urlparse(str(url)).netloc.lower().removeprefix("www.")
        return {
            "provider": provider,
            "query": query,
            "title": item.get("title") or item.get("name") or "Untitled result",
            "url": url,
            "source": source or provider,
            "published_date": item.get("date") or item.get("publishedDate") or "",
            "summary": item.get("snippet") or item.get("content") or "",
        }
