# -*- coding: utf-8 -*-
"""Portfolio 新闻研究管线（修改计划第三轮 10~14 / 25）。

解决的问题：
- 相关性匹配绝不使用 query（修改计划 10）：之前 query 必含 ticker 导致一切命中；
- 时效分层（修改计划 11）：fresh_event / recent_background / structural / stale；
  "4 days ago" 转绝对日期；未知日期降权；
- 证据类型（修改计划 12）：event / reference / macro，ETF 产品页/事实表归 reference；
- 来源与搜索提供方分离（修改计划 14）：source_name / source_domain vs search_provider；
- 证据配额（修改计划 25）：按优先级精选后输入 Agent，原始结果另存。
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from .research_service import ResearchService
from .article_fetcher import _fetch_article_text
from .tools import _source_domain, _source_quality_score, _evidence_grade


# ── 查询生成（instrument-aware；query 仅用于搜索，绝不参与相关性匹配）──
def build_instrument_aware_queries(
    top_risk_tickers: list[str],
    instrument_metadata: dict[str, dict[str, Any]],
    benchmark: str = "^GSPC",
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for ticker in top_risk_tickers:
        meta = instrument_metadata.get(ticker, {})
        name = (meta.get("name") or ticker)
        itype = str(meta.get("instrument_type") or "UNKNOWN").upper()
        underlying = meta.get("underlying_index")
        theme = meta.get("theme")

        if itype == "EQUITY":
            queries.append({"query": f"{name} {ticker} latest earnings guidance revenue margin risks", "scope": "ticker", "ticker": ticker, "event_hint": "earnings"})
            queries.append({"query": f"{name} {ticker} analyst revisions regulatory litigation latest", "scope": "ticker", "ticker": ticker, "event_hint": "rating"})
        elif itype == "ETF":
            if underlying:
                queries.append({"query": f"{name} {underlying} latest outlook", "scope": "ticker", "ticker": ticker, "event_hint": "outlook"})
                queries.append({"query": f"{underlying} fund flows valuation sector risks", "scope": "ticker", "ticker": ticker, "event_hint": "flows"})
            else:
                queries.append({"query": f"{name} ETF latest outlook fund flows", "scope": "ticker", "ticker": ticker, "event_hint": "outlook"})
        elif itype == "ETC":
            queries.append({"query": f"{name} commodity latest drivers real yields USD central bank demand", "scope": "ticker", "ticker": ticker, "event_hint": "commodity"})
            queries.append({"query": f"commodity ETF ETC flows latest {name}", "scope": "ticker", "ticker": ticker, "event_hint": "flows"})
        elif itype == "CRYPTO":
            queries.append({"query": f"{name} latest regulation ETF flows liquidity market risk", "scope": "ticker", "ticker": ticker, "event_hint": "crypto"})
            queries.append({"query": f"{name} macro correlation institutional flows", "scope": "ticker", "ticker": ticker, "event_hint": "macro"})
        elif itype == "INDEX":
            idx = underlying or name
            queries.append({"query": f"{idx} latest earnings breadth valuation outlook", "scope": "ticker", "ticker": ticker, "event_hint": "breadth"})
        else:
            queries.append({"query": f"{name} {ticker} latest news risks", "scope": "ticker", "ticker": ticker, "event_hint": "general"})

    # 宏观 / 基准（绝不以账户分组作为搜索对象）
    queries.append({"query": f"{benchmark} interest rates macro market risk outlook", "scope": "macro", "ticker": None, "event_hint": "macro"})
    return queries[:14]


# ── 过滤（账户平台 / 纯行情 / 低质量）──────────────────────────
_LOW_QUALITY_HINTS = [
    "coupon", "login", "sign in", "forum", "reddit", "pinterest", "youtube",
    "stocktwits", "wikipedia", "dictionary", "pdfcoffee", "facebook", "instagram", "tiktok",
    "careers", "about us", "contact",
]
_ACCOUNT_PLATFORM_HINTS = ["trade republic", "trading212", "trading 212", "broker", "brokerage account"]
_QUOTE_ONLY_HINTS = ["quote", "stock price", "historische kurse", "real-time quote"]


def _looks_like_account_platform(title: str, url: str) -> bool:
    text = (title + " " + url).lower()
    return any(h in text for h in _ACCOUNT_PLATFORM_HINTS)


def _looks_like_quote_or_overview(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    if any(h in text for h in _QUOTE_ONLY_HINTS):
        has_event = re.search(r"(earnings|revenue|guidance|downgrade|upgrade|lawsuit|recall|fda|merger|acquisition|dividend|guidance|miss|beat|cut|raise)", text)
        if not has_event:
            return True
    return False


def _manager_from_name(name: str) -> str | None:
    low = (name or "").lower()
    for mgr in ("ishares", "vanguard", "invesco", "xtrackers", "spdr", "lyxor", "amundi",
                "vaneck", "wisdomtree", "db x-trackers", "comstage", "franklin", "bnp", "hsbc"):
        if mgr in low:
            return mgr
    return None


def _entity_aliases(ticker: str, name: str) -> list[str]:
    aliases = [ticker, name]
    shortened = re.sub(
        r"\b(incorporated|inc\.?|corporation|corp\.?|plc|ltd\.?|limited|se|ag|class\s+[a-z])\b",
        " ", name, flags=re.I,
    )
    shortened = re.sub(r"[,\s]+", " ", shortened).strip()
    if shortened:
        aliases.append(shortened)
    first = shortened.split()[0] if shortened else ""
    if len(first) >= 4:
        aliases.append(first)
    return list(dict.fromkeys(a.strip().lower() for a in aliases if a and a.strip()))


def _is_relevant_to_ticker(result: dict[str, Any], ticker: str | None, meta: dict[str, Any]) -> bool:
    """相关性匹配（修改计划 10）：绝不使用 query。

    仅基于 title / summary / url 与 ticker 的精确信息匹配。
    """
    if not ticker:
        return True
    text = " ".join([
        str(result.get("title") or ""), str(result.get("summary") or ""),
        str(result.get("url") or ""),
    ]).lower()
    name = str(meta.get("name") or ticker)
    itype = str(meta.get("instrument_type") or "UNKNOWN").upper()

    if itype == "EQUITY":
        for alias in _entity_aliases(ticker, name):
            cleaned = re.sub(rf"\bunrelated\s+to\s+{re.escape(alias)}\b", "", text, flags=re.I)
            if alias in cleaned:
                return True
        return False
    if itype in ("ETF", "ETC", "FUND", "INDEX"):
        if ticker.lower() in text:
            return True
        if name and name.lower() in text:
            return True
        underlying = str(meta.get("underlying_index") or "").lower()
        theme = str(meta.get("theme") or "").lower()
        if underlying and underlying in text:
            return True
        if theme and theme in text:
            return True
        manager = _manager_from_name(name)
        product_tokens = [
            token for token in re.findall(r"[a-z0-9]{4,}", f"{underlying} {theme}")
            if token not in {"index", "fund", "ucits", "equity"}
        ]
        if manager and manager in text and any(token in text for token in product_tokens):
            return True
        return False
    if itype == "CRYPTO":
        asset = str(meta.get("name") or ticker).lower()
        base = ticker.split("-")[0].lower() if "-" in ticker else ticker.lower()
        if asset in text or base in text:
            return True
        return False
    # UNKNOWN：宽松匹配 ticker/name
    return (ticker.lower() in text) or bool(name and name.lower() in text)


def filter_candidates_with_diagnostics(
    candidates: list[dict[str, Any]],
    instrument_metadata: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    reasons = {
        "missing_url": 0, "duplicate": 0, "account_platform": 0,
        "quote_only": 0, "low_quality": 0, "entity_mismatch": 0,
    }
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in candidates:
        url = str(c.get("url") or "").strip()
        title = str(c.get("title") or "").strip()
        summary = str(c.get("summary") or "")
        ticker = c.get("ticker")
        meta = instrument_metadata.get(ticker, {}) if ticker else {}
        reason = None
        if not url or url.lower().startswith("javascript:"):
            reason = "missing_url"
        elif url in seen_urls or title in seen_titles:
            reason = "duplicate"
        elif _looks_like_account_platform(title, url):
            reason = "account_platform"
        elif _looks_like_quote_or_overview(title, summary):
            reason = "quote_only"
        elif any(h in (title + " " + url).lower() for h in _LOW_QUALITY_HINTS):
            reason = "low_quality"
        elif ticker and not _is_relevant_to_ticker(c, ticker, meta):
            reason = "entity_mismatch"
        if reason:
            reasons[reason] += 1
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        out.append(c)
    return out, reasons


def filter_candidates(
    candidates: list[dict[str, Any]],
    instrument_metadata: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return filter_candidates_with_diagnostics(candidates, instrument_metadata)[0]


# ── 日期与时效（修改计划 11）──────────────────────────────────
_FRESH_DAYS = int(os.environ.get("PORTFOLIO_RESEARCH_FRESH_DAYS", "45") or "45")
_BACKGROUND_DAYS = int(os.environ.get("PORTFOLIO_RESEARCH_BACKGROUND_DAYS", "120") or "120")
_MAX_AGE_DAYS = int(os.environ.get("PORTFOLIO_RESEARCH_MAX_AGE_DAYS", "365") or "365")
_REQUIRE_VERIFIED_ARTICLE = os.environ.get(
    "PORTFOLIO_RESEARCH_REQUIRE_VERIFIED_ARTICLE", "false",
).strip().lower() in {"1", "true", "yes"}


def _normalize_date(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d+)\s+days?\s+ago", s, re.I)
    if m:
        return (date.today() - timedelta(days=int(m.group(1)))).isoformat()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s[: len(fmt) + 2] if False else s[:30], fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except (ValueError, TypeError):
        return s


def _recency_tier(published_date: str) -> str:
    s = _normalize_date(published_date)
    if not s or len(s) < 10:
        return "unknown"
    try:
        d = date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return "unknown"
    age = (date.today() - d).days
    if age <= _FRESH_DAYS:
        return "fresh_event"
    if age <= _BACKGROUND_DAYS:
        return "recent_background"
    if age <= _MAX_AGE_DAYS:
        return "structural"
    return "stale"


def _evidence_kind(result: dict[str, Any], scope: str | None, meta: dict[str, Any]) -> str:
    if scope == "macro" or not result.get("ticker"):
        return "macro"
    url = (result.get("url") or "").lower()
    title = (result.get("title") or "").lower()
    path = urlparse(url).path.rstrip("/").lower()
    if (
        path in {"", "/news"}
        or any(k in title for k in ["investor relations", "quarterly results", "financials - quarterly"])
        or path.endswith("/quarterly-results/default.aspx")
        or "/quote/" in path and path.endswith("/news")
    ):
        return "reference"
    if any(k in url for k in ["factsheet", "/portfolio/", "holdings", "prospectus"]) or "factsheet" in title:
        return "reference"
    if any(k in url for k in ["product", "overview"]) and "news" not in url:
        # 产品/概览页而非新闻稿
        if any(k in title for k in ["etf", "fund", "stock", "share", "profile"]):
            return "reference"
    return "event"


# ── 影响方向 / 范围推断 ───────────────────────────────────────
_NEG = ["downgrade", "miss", "cut", "lower", "weak", "loss", "lawsuit", "probe", "recall", "decline", "drop", "fall", "risk", "warning", "default", "下调", "降级", "亏损", "诉讼", "风险"]
_POS = ["upgrade", "beat", "raise", "higher", "strong", "profit", "growth", "record", "approval", "win", "gain", "outperform", "上调", "超预期", "盈利", "增长"]


def _infer_impact(text: str) -> tuple[str, str]:
    low = (text or "").lower()
    neg = sum(1 for w in _NEG if w in low)
    pos = sum(1 for w in _POS if w in low)
    direction = "neutral"
    if pos > neg:
        direction = "positive"
    elif neg > pos:
        direction = "negative"
    horizon = "short_term" if any(w in low for w in ["today", "q2", "q3", "earnings", "immediate", "短期"]) else "medium_term"
    if any(w in low for w in ["long-term", "outlook", "secular", "structural", "2026", "2027", "长期"]):
        horizon = "long_term"
    return direction, horizon


def _summarize_zh(facts: list[str], title: str, source_name: str, published_date: str) -> str:
    """结构化中文摘要（修改计划 13 的确定性版本）。

    明确标注来源与日期，先列事实再补标题；事实来自英文源时如实呈现，
    不做臆测翻译。后续可接 Evidence Summarizer（LLM）做真中文改写。
    """
    head = f"【{source_name or '未知来源'}"
    if published_date:
        head += f" · {published_date}"
    head += "】"
    if facts:
        body = "；".join(facts[:4])
        return head + body + "。"
    if title:
        return head + title + "。"
    return head + "（无可用摘要）"


def _tier_from_grade(grade: str) -> str:
    return {"A": "tier_1", "B": "tier_2", "C": "tier_3", "D": "tier_3", "TECH": "tier_2"}.get(grade, "tier_3")


def _priority_score(note: dict[str, Any]) -> float:
    source_q = float(note.get("source_quality_score") or 0) / 100.0
    recency = {"fresh_event": 1.0, "recent_background": 0.7, "structural": 0.4, "stale": 0.1, "unknown": 0.2}.get(note.get("recency_tier"), 0.2)
    relevance = 0.8 if note.get("ticker") else 0.5
    specificity = 0.6 if note.get("evidence_kind") == "event" else 0.4
    return round(source_q * 0.30 + relevance * 0.30 + recency * 0.25 + specificity * 0.15, 4)


def _dated_snippet_fallback_ok(note: dict[str, Any]) -> bool:
    """Allow a concrete, dated search snippet as explicitly unverified evidence.

    Generic/reference pages are removed before this point.  This fallback is
    intentionally limited to recent event/macro results from non-tier-3 sources
    with enough concrete snippet text to be useful.  It never upgrades a search
    snippet to verified article content.
    """
    if note.get("recency_tier") not in {"fresh_event", "recent_background"}:
        return False
    if note.get("evidence_kind") not in {"event", "macro"}:
        return False
    if str(note.get("source_quality") or "tier_3") == "tier_3":
        return False
    if float(note.get("source_quality_score") or 0.0) < 60.0:
        return False
    facts_text = " ".join(str(fact).strip() for fact in (note.get("facts") or []) if str(fact).strip())
    return len(facts_text) >= 40


def build_evidence_notes(
    candidates: list[dict[str, Any]],
    instrument_metadata: dict[str, dict[str, Any]],
    *,
    max_articles: int = 12,
    max_evidence: int = 15,
    search_provider: str = "unknown",
    fetch_timeout: float = 12,
) -> list[dict[str, Any]]:
    """把候选结果转换为结构化 Evidence Notes（含中文摘要、影响方向、时效、类型）。"""
    notes: list[dict[str, Any]] = []
    for c in candidates:
        ticker = c.get("ticker")
        meta = instrument_metadata.get(ticker, {}) if ticker else {}
        url = str(c.get("url") or "").strip()
        title = str(c.get("title") or "")
        source_name = str(c.get("source") or _source_domain(url) or "unknown")
        published_date = _normalize_date(c.get("published_date"))
        summary = str(c.get("summary") or "")
        facts_raw = re.split(r"(?<=[.!?])\s+", summary)
        facts = [f.strip() for f in facts_raw if len(f.strip()) > 12][:4]
        score = _source_quality_score({"url": url, "title": title, "facts": summary, "source_date": published_date})
        grade = _evidence_grade({"url": url, "title": title, "facts": summary, "source_date": published_date, "source_quality_score": score})
        direction, horizon = _infer_impact(title + " " + summary)
        evidence_kind = _evidence_kind(c, c.get("scope"), meta)

        note = {
            "evidence_id": "",  # 稍后统一编号
            "scope": c.get("scope") or ("ticker" if ticker else "portfolio"),
            "ticker": ticker,
            "related_tickers": [ticker] if ticker else [],
            "event_type": c.get("event_hint") or "general",
            "evidence_kind": evidence_kind,
            "title": title,
            "source_name": source_name,
            "source_domain": _source_domain(url),
            "search_provider": search_provider,
            "published_date": published_date,
            "url": url,
            "source_quality": _tier_from_grade(grade),
            "source_quality_score": score,
            "facts": facts,
            "summary_zh": _summarize_zh(facts, title, source_name, published_date),
            "impact_direction": direction,
            "impact_horizon": horizon,
            "recency_tier": _recency_tier(published_date),
            "portfolio_relevance": _relevance_note(ticker, meta),
            "confidence": round(min(0.95, max(0.4, score / 100.0)), 2),
            "article_fetch_ok": False,
            "content_basis": "search_snippet_unverified",
            "verification_status": "search_snippet_unverified",
        }

        note["priority_score"] = _priority_score(note)
        notes.append(note)

    # 先按元数据打分和每 ticker 配额精选，再抓正文，避免低价值候选耗尽抓取预算。
    notes.sort(key=lambda n: n.get("priority_score", 0), reverse=True)
    limit = 15 if max_evidence is None else max(1, int(max_evidence))
    selected: list[dict[str, Any]] = []
    ticker_counts: dict[str, int] = {}
    macro_count = 0
    # 第一轮保证每个有候选的 top-risk ticker 至少一条。
    for note in notes:
        ticker = str(note.get("ticker") or "")
        if ticker and ticker_counts.get(ticker, 0) == 0:
            selected.append(note)
            ticker_counts[ticker] = 1
            if len(selected) >= limit:
                break
    # 第二轮按分数填充：单 ticker 最多 3 条，宏观最多 3 条。
    for note in notes:
        if note in selected or len(selected) >= limit:
            continue
        ticker = str(note.get("ticker") or "")
        if ticker:
            if ticker_counts.get(ticker, 0) >= 3:
                continue
            ticker_counts[ticker] = ticker_counts.get(ticker, 0) + 1
        else:
            if macro_count >= 3:
                continue
            macro_count += 1
        selected.append(note)

    article_slots = 0
    for note in selected:
        url = str(note.get("url") or "")
        ticker = note.get("ticker")
        note_meta = instrument_metadata.get(ticker, {}) if ticker else {}
        if not url or article_slots >= max_articles or float(note.get("source_quality_score") or 0) < 60:
            continue
        try:
            art = _fetch_article_text(url, timeout=fetch_timeout, max_chars=4000)
            if art.get("ok") and art.get("article_text_quality_ok"):
                art_text = str(art.get("text") or "")
                if ticker and not _is_relevant_to_ticker(
                    {"title": "", "summary": art_text, "url": ""}, ticker, note_meta,
                ):
                    note["article_fetch_error"] = "content_entity_mismatch"
                    article_slots += 1
                    continue
                facts2 = [s.strip() for s in re.split(r"(?<=[.!?])\s+", art_text) if len(s.strip()) > 30][:4]
                if facts2:
                    note["article_fetch_ok"] = True
                    note["content_basis"] = "article_body"
                    note["verification_status"] = "article_body_verified"
                    note["facts"] = facts2
                final_url = str(art.get("final_url") or "").strip()
                if final_url:
                    note["url"] = final_url
                    note["source_domain"] = _source_domain(final_url)
                article_date = _normalize_date(art.get("published_date"))
                if article_date and not note.get("published_date"):
                    note["published_date"] = article_date
                    note["recency_tier"] = _recency_tier(article_date)
                if facts2:
                    note["summary_zh"] = _summarize_zh(
                        note["facts"], note.get("title", ""), note.get("source_name", ""),
                        note.get("published_date", ""),
                    )
                # 关键词推断只保留为初步提示，最终方向由 Evidence Summarizer 覆盖。
                note["preliminary_impact_hint"] = _infer_impact(str(note.get("title") or "") + " " + art_text)[0]
            elif art.get("ok"):
                note["article_fetch_error"] = str(art.get("quality_reason") or "article_text_quality_failed")
        except Exception as exc:  # noqa: BLE001
            note["article_fetch_error"] = type(exc).__name__
        article_slots += 1
    # 统一编号 + 再按编号稳定排序
    selected.sort(key=lambda n: n.get("priority_score", 0), reverse=True)
    for i, n in enumerate(selected, start=1):
        n["evidence_id"] = f"E{i:03d}"
    return selected


def _relevance_note(ticker: str | None, meta: dict[str, Any]) -> str:
    if not ticker:
        return "组合层面的宏观/系统性因素，影响全部持仓的风险偏好与贴现率。"
    itype = str(meta.get("instrument_type") or "UNKNOWN").upper()
    theme = meta.get("theme") or meta.get("underlying_index") or ""
    if itype == "EQUITY":
        return f"直接影响个股 {ticker} 的盈利、估值或情绪；也可能外溢至同行业/同主题持仓。"
    if itype == "ETF":
        return f"影响 ETF {ticker}（{theme}）的净值与资金流；若该主题为组合集中暴露，则放大系统性波动。"
    if itype == "ETC":
        return f"影响商品 ETC {ticker} 的标的价格与避险属性。"
    if itype == "CRYPTO":
        return f"影响加密资产 {ticker} 的价格与流动性，并外溢至风险偏好。"
    if itype == "INDEX":
        return f"影响指数 {ticker}（{theme}），进而作用于组合基准与宽基暴露。"
    return f"与 {ticker} 相关，需结合其工具类型判断对组合的具体影响。"


class PortfolioResearchService:
    """组合新闻研究服务。"""

    def __init__(self, provider: str = "auto"):
        self._service = ResearchService()
        self.provider = provider

    def research(
        self,
        top_risk_tickers: list[str],
        instrument_metadata: dict[str, dict[str, Any]],
        benchmark: str = "^GSPC",
        *,
        max_results_per_query: int = 3,
        max_articles: int = 15,
        max_evidence: int = 15,
    ) -> dict[str, Any]:
        queries = build_instrument_aware_queries(top_risk_tickers, instrument_metadata, benchmark=benchmark)
        raw: list[dict[str, Any]] = []
        query_stats: list[dict[str, Any]] = []
        provider_errors: list[str] = []
        provider_used = None
        for q in queries:
            search_result = self._service.search(
                [q["query"]], provider=self.provider, max_results=max_results_per_query,
                recency_days=_BACKGROUND_DAYS,
            )
            results = list(search_result.get("results") or [])
            diag = search_result.get("diagnostics") or {}
            provider_used = provider_used or diag.get("provider_used")
            provider_errors.extend(diag.get("errors") or [])
            for r in results:
                r.setdefault("scope", q["scope"])
                r.setdefault("ticker", q["ticker"])
                r.setdefault("event_hint", q["event_hint"])
            raw.extend(results)
            query_stats.append({
                "ticker": q["ticker"], "scope": q["scope"], "query": q["query"],
                "raw_count": len(results), "accepted_count": 0, "rejected": {},
            })
        filtered, rejected = filter_candidates_with_diagnostics(raw, instrument_metadata)
        accepted_by_query: dict[str, int] = {}
        for item in filtered:
            query = str(item.get("query") or "")
            accepted_by_query[query] = accepted_by_query.get(query, 0) + 1
        for stat in query_stats:
            stat["accepted_count"] = accepted_by_query.get(str(stat.get("query") or ""), 0)
        recent_filtered: list[dict[str, Any]] = []
        reference_count = outside_window_count = 0
        for candidate in filtered:
            ticker = candidate.get("ticker")
            meta = instrument_metadata.get(ticker, {}) if ticker else {}
            if _evidence_kind(candidate, candidate.get("scope"), meta) == "reference":
                reference_count += 1
                continue
            tier = _recency_tier(candidate.get("published_date"))
            if tier not in {"fresh_event", "recent_background", "unknown"}:
                outside_window_count += 1
                continue
            recent_filtered.append(candidate)
        selected = build_evidence_notes(
            recent_filtered, instrument_metadata,
            max_articles=max_articles, max_evidence=max_evidence,
            search_provider=self.provider,
        )
        unresolved_date_count = sum(1 for item in selected if item.get("recency_tier") == "unknown")
        selected_recent = [
            item for item in selected
            if item.get("recency_tier") in {"fresh_event", "recent_background"}
        ]
        unverified_count = sum(1 for item in selected_recent if not item.get("article_fetch_ok"))
        if _REQUIRE_VERIFIED_ARTICLE:
            evidence = [item for item in selected_recent if item.get("article_fetch_ok")]
        else:
            evidence = [
                item for item in selected_recent
                if item.get("article_fetch_ok") or _dated_snippet_fallback_ok(item)
            ]
            for item in evidence:
                if not item.get("article_fetch_ok"):
                    item["content_basis"] = "search_snippet_unverified"
                    item["verification_status"] = "search_snippet_unverified"
                    item["confidence"] = round(min(0.60, float(item.get("confidence") or 0.40)), 2)
        snippet_fallback_count = sum(1 for item in evidence if not item.get("article_fetch_ok"))
        snippet_too_weak_count = sum(
            1 for item in selected_recent
            if not item.get("article_fetch_ok") and not _dated_snippet_fallback_ok(item)
        )
        raw_count = len(raw)
        filtered_count = len(recent_filtered)
        covered = {str(e.get("ticker")) for e in evidence if e.get("ticker")}
        coverage = len(set(top_risk_tickers) & covered) / len(top_risk_tickers) if top_risk_tickers else 1.0
        if not raw_count:
            status = "not_configured" if any("not configured" in e for e in provider_errors) else ("provider_error" if provider_errors else "no_raw_results")
        elif not filtered:
            status = "all_filtered"
        elif not recent_filtered:
            status = "no_recent_evidence"
        elif not evidence:
            status = "all_filtered"
        elif coverage < float(os.environ.get("PORTFOLIO_REPORT_MIN_NEWS_COVERAGE", "0.60")):
            status = "insufficient_coverage"
        else:
            status = "success"
        diagnostics = {
            "status": status,
            "provider_requested": self.provider,
            "provider_used": provider_used,
            "queries_count": len(queries),
            "raw_results_count": raw_count,
            "filtered_results_count": filtered_count,
            "selected_evidence_count": len(evidence),
            "top_risk_coverage": round(coverage, 3),
            "errors": list(dict.fromkeys(provider_errors)),
            "rejected": {
                **rejected,
                "reference_page": reference_count,
                "unknown_date": unresolved_date_count,
                "outside_recent_window": outside_window_count,
                "article_unverified": unverified_count,
                "snippet_too_weak": snippet_too_weak_count,
            },
            "verified_article_count": sum(1 for item in evidence if item.get("article_fetch_ok")),
            "snippet_fallback_count": snippet_fallback_count,
            "strict_article_verification": _REQUIRE_VERIFIED_ARTICLE,
            "query_stats": query_stats,
        }
        return {
            "status": status,
            "evidence": evidence,
            "diagnostics": diagnostics,
            "raw_results": raw,
            "filtered_results": recent_filtered,
        }
