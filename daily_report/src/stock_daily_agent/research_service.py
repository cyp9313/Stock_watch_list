from __future__ import annotations

import os
from typing import Any

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
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for query in queries:
            evidence.extend(self._search_one(query, provider=provider, max_results=max_results))
        for index, item in enumerate(evidence, start=1):
            item.setdefault("evidence_id", f"E{index:03d}")
        return evidence

    def _search_one(self, query: str, *, provider: str, max_results: int) -> list[dict[str, Any]]:
        provider = (provider or "auto").lower()
        if provider in {"auto", "priority", "serper", "both"} and os.environ.get("SERPER_API_KEY"):
            results = self._serper_search(query, max_results=max_results)
            if results or provider == "serper":
                return results
        if provider in {"auto", "priority", "searxng", "both"} and os.environ.get("SEARXNG_URL"):
            return self._searxng_search(query, max_results=max_results)
        return []

    def _serper_search(self, query: str, *, max_results: int) -> list[dict[str, Any]]:
        endpoint = os.environ.get("SERPER_ENDPOINT", "https://google.serper.dev/search")
        headers = {"X-API-KEY": os.environ["SERPER_API_KEY"], "Content-Type": "application/json"}
        response = requests.post(endpoint, headers=headers, json={"q": query, "num": max_results}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("organic") or []
        return [self._normalize_result(item, query, "serper") for item in items[:max_results]]

    def _searxng_search(self, query: str, *, max_results: int) -> list[dict[str, Any]]:
        base = os.environ["SEARXNG_URL"].rstrip("/")
        response = requests.get(
            f"{base}/search",
            params={"q": query, "format": "json", "language": os.environ.get("SEARXNG_LANGUAGE", "auto")},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("results") or []
        return [self._normalize_result(item, query, "searxng") for item in items[:max_results]]

    @staticmethod
    def _normalize_result(item: dict[str, Any], query: str, provider: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "query": query,
            "title": item.get("title") or item.get("name") or "Untitled result",
            "url": item.get("link") or item.get("url") or "",
            "source": item.get("source") or item.get("sitename") or provider,
            "published_date": item.get("date") or item.get("publishedDate") or "",
            "summary": item.get("snippet") or item.get("content") or "",
        }
