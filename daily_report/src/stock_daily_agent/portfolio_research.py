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
from .research_core.evidence_id import make_evidence_uid
from .research_query_planner import build_ai_research_plan
from .research_plan_validator import validate_research_plan
from .research_query_compiler import (
    compile_research_queries,
    expand_official_lane_queries,
)
from .research_gap_analyzer import analyze_research_gap, MAX_GAP_QUERIES


# ── 第六轮入口：AI Research Query Planner ───────────────────
__all__ = [
    "build_instrument_aware_queries",
    "filter_candidates",
    "filter_candidates_with_diagnostics",
    "build_evidence_notes",
    "PortfolioResearchService",
    "build_ai_research_plan",
    "validate_research_plan",
    "compile_research_queries",
    "expand_official_lane_queries",
]


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


def _entity_aliases(ticker: str, name: str, extra_aliases: list[str] | None = None) -> list[str]:
    aliases = [ticker, ticker.split(".", 1)[0], name]
    aliases.extend(str(item) for item in (extra_aliases or []) if str(item).strip())
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
        for alias in _entity_aliases(ticker, name, meta.get("entity_aliases") or []):
            cleaned = re.sub(rf"\bunrelated\s+to\s+{re.escape(alias)}\b", "", text, flags=re.I)
            if re.fullmatch(r"[a-z0-9.-]{1,6}", alias):
                if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", cleaned):
                    return True
            elif alias in cleaned:
                return True
        return False
    if itype in ("ETF", "ETC", "FUND", "INDEX"):
        ticker_aliases = [ticker.lower(), ticker.split(".", 1)[0].lower()]
        if any(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) for alias in ticker_aliases if alias):
            return True
        product_aliases = [name] + list(meta.get("entity_aliases") or [])
        if any(str(alias).lower() in text for alias in product_aliases if str(alias).strip()):
            return True
        underlying = str(meta.get("underlying_index") or "").lower()
        theme = str(meta.get("theme") or "").lower()
        if underlying and underlying in text:
            return True
        if theme and theme in text:
            return True
        # §11 修复：key_driver token 级匹配（如 "uranium mine supply" 匹配 "uranium supply outage"）
        key_drivers = meta.get("key_drivers") or []
        if key_drivers:
            driver_text = " ".join(key_drivers).lower()
            driver_tokens = {t for t in re.findall(r"[a-z]{4,}", driver_text)
                             if t not in {"supply", "demand", "policy", "risk", "market", "price", "growth", "outlook"}}
            text_tokens = set(re.findall(r"[a-z]{4,}", text.lower()))
            overlap = driver_tokens & text_tokens
            if len(overlap) >= 2:
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
    *,
    exclude_terms: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    reasons = {
        "missing_url": 0, "duplicate": 0, "merged_duplicate": 0, "account_platform": 0,
        "quote_only": 0, "low_quality": 0, "entity_mismatch": 0,
        "excluded_term": 0,
    }
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    url_map: dict[str, dict] = {}  # §22: URL→entry for merging related_tickers
    out: list[dict[str, Any]] = []
    exclude_lower = [e.lower() for e in (exclude_terms or []) if e]
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
            # §22: 如果是同一 URL 但不同 ticker，合并 related_tickers 而非直接删除
            if url and url in url_map:
                existing = url_map[url]
                existing_ticker = existing.get("ticker")
                if ticker and ticker != existing_ticker:
                    related = list(existing.get("related_tickers") or [])
                    if existing_ticker and existing_ticker not in related:
                        related.append(existing_ticker)
                    if ticker not in related:
                        related.append(ticker)
                    existing["related_tickers"] = related
                    existing.setdefault("matched_questions", []).extend(c.get("matched_questions") or [])
                    reasons["merged_duplicate"] += 1
                    continue
            reason = "duplicate"
        elif _looks_like_account_platform(title, url):
            reason = "account_platform"
        elif _looks_like_quote_or_overview(title, summary):
            reason = "quote_only"
        elif any(h in (title + " " + url).lower() for h in _LOW_QUALITY_HINTS):
            reason = "low_quality"
        elif exclude_lower and any(ex in (title + " " + summary).lower() for ex in exclude_lower):
            reason = "excluded_term"
        elif ticker and not _is_relevant_to_ticker(c, ticker, meta):
            reason = "entity_mismatch"
        if reason:
            reasons[reason] += 1
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        out.append(c)
        if url:
            url_map[url] = c
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


def _normalize_date(
    value: Any,
    *,
    reference_datetime: datetime | None = None,
) -> str:
    """Normalize provider dates to ``YYYY-MM-DD``.

    Search providers commonly return relative values such as ``21 hours ago``.
    These must be resolved before recency scoring or latest-event selection; slicing
    the raw string to ten characters previously produced values such as
    ``21 hours a``.  Unknown values now return an empty string and the original
    provider value is retained separately in ``raw_published_date``.
    """
    s = str(value or "").strip()
    if not s:
        return ""

    ref = reference_datetime or datetime.now().astimezone()
    low = s.lower()
    if low in {"today", "just now", "now"}:
        return ref.date().isoformat()
    if low == "yesterday":
        return (ref - timedelta(days=1)).date().isoformat()

    relative = re.search(
        r"(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\s+ago",
        s,
        re.I,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        if unit.startswith(("minute", "min")):
            delta = timedelta(minutes=amount)
        elif unit.startswith(("hour", "hr")):
            delta = timedelta(hours=amount)
        elif unit.startswith("week"):
            delta = timedelta(weeks=amount)
        elif unit.startswith("month"):
            # Search snippets generally use approximate month ages; preserve that
            # convention rather than inventing a calendar day.
            delta = timedelta(days=30 * amount)
        else:
            delta = timedelta(days=amount)
        return (ref - delta).date().isoformat()

    # ISO timestamps and ISO dates are the most common absolute forms.
    try:
        parsed_iso = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return parsed_iso.date().isoformat()
    except (ValueError, TypeError):
        pass
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except (ValueError, TypeError):
        pass

    for fmt in (
        "%Y/%m/%d",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m",
        "%Y",
    ):
        try:
            return datetime.strptime(s[:40], fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    return ""


def _coerce_reference_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.astimezone()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.astimezone()
    except (ValueError, TypeError):
        return None


def _recency_tier(
    published_date: str,
    *,
    reference_datetime: datetime | None = None,
) -> str:
    ref = reference_datetime or datetime.now().astimezone()
    s = _normalize_date(published_date, reference_datetime=ref)
    if not s or len(s) < 10:
        return "unknown"
    try:
        d = date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return "unknown"
    age = (ref.date() - d).days
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
    max_candidates_per_ticker: int = 3,
    preselection_multiplier: int = 1,
    reference_datetime: datetime | None = None,
) -> list[dict[str, Any]]:
    """把候选结果转换为结构化 Evidence Notes（含中文摘要、影响方向、时效、类型）。"""
    notes: list[dict[str, Any]] = []
    for c in candidates:
        ticker = c.get("ticker")
        meta = instrument_metadata.get(ticker, {}) if ticker else {}
        url = str(c.get("url") or "").strip()
        title = str(c.get("title") or "")
        source_name = str(c.get("source") or _source_domain(url) or "unknown")
        raw_published_date = str(c.get("published_date") or "").strip()
        date_reference = (
            _coerce_reference_datetime(c.get("search_retrieved_at"))
            or reference_datetime
            or datetime.now().astimezone()
        )
        published_date = _normalize_date(
            raw_published_date,
            reference_datetime=date_reference,
        )
        summary = str(c.get("summary") or "")
        facts_raw = re.split(r"(?<=[.!?])\s+", summary)
        facts = [f.strip() for f in facts_raw if len(f.strip()) > 12][:4]
        score = _source_quality_score({"url": url, "title": title, "facts": summary, "source_date": published_date})
        grade = _evidence_grade({"url": url, "title": title, "facts": summary, "source_date": published_date, "source_quality_score": score})
        direction, horizon = _infer_impact(title + " " + summary)
        evidence_kind = _evidence_kind(c, c.get("scope"), meta)

        # 第七轮第 3 节：子流程只生成稳定 evidence_uid，最终 evidence_id 由收口点统一分配。
        note = {
            "evidence_id": None,  # 收口点（finalize_evidence_ids）统一编号
            "evidence_uid": make_evidence_uid({
                "url": url, "ticker": ticker,
                "published_date": published_date, "title": title,
            }),
            "scope": c.get("scope") or ("ticker" if ticker else "portfolio"),
            "ticker": ticker,
            "related_tickers": [ticker] if ticker else [],
            "event_type": c.get("event_hint") or c.get("event_need") or "general_event",
            "event_hint": c.get("event_hint") or c.get("event_need"),
            "question_id": c.get("question_id"),
            "lane": c.get("lane") or "news",
            "vertical": c.get("vertical"),
            "gap_search": bool(c.get("gap_search")),
            "evidence_kind": evidence_kind,
            "title": title,
            "raw_title": title,
            "raw_snippet": summary,
            "raw_url": url,
            "raw_published_date": raw_published_date,
            "date_reference_datetime": date_reference.isoformat(),
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
            "recency_tier": _recency_tier(
                published_date, reference_datetime=date_reference,
            ),
            "portfolio_relevance": _relevance_note(ticker, meta),
            "confidence": round(min(0.95, max(0.4, score / 100.0)), 2),
            "article_fetch_ok": False,
            "snippet_fallback_ok": False,
            "content_basis": "search_snippet_unverified",
            "verification_status": "search_snippet_unverified",
        }

        note["priority_score"] = _priority_score(note)

        # §14 修复：Source Quality 分类（source_type + content_type）
        from .research_core.source_classifier import classify_source_quality
        sq = classify_source_quality(note, meta=meta)
        note["source_type"] = sq["source_type"]
        note["content_type"] = sq["content_type"]
        # Opinion / forecast 类内容封顶置信度
        if sq["content_type"] == "opinion":
            note["confidence"] = min(note["confidence"], 0.50)

        notes.append(note)

    # Materiality 前只做宽松预选。Official/Regulator 候选有独立保底位，
    # 避免前三条通用新闻把真正的 IR/监管事件挤出正文抓取预算。
    notes.sort(
        key=lambda n: (
            str(n.get("source_type") or "") in {"official", "regulator", "rating_agency"},
            str(n.get("lane") or "") in {"official", "official_and_news"},
            float(n.get("priority_score") or 0.0),
        ),
        reverse=True,
    )
    final_limit = 15 if max_evidence is None else max(1, int(max_evidence))
    preselection_limit = max(final_limit, final_limit * max(1, int(preselection_multiplier)))
    per_ticker_limit = max(1, int(max_candidates_per_ticker))
    selected: list[dict[str, Any]] = []
    ticker_counts: dict[str, int] = {}
    macro_count = 0

    # 每个 ticker 最多保留 2 条官方/监管候选。
    for note in notes:
        ticker = str(note.get("ticker") or "")
        is_official = str(note.get("source_type") or "") in {"official", "regulator", "rating_agency"} \
            or str(note.get("lane") or "") in {"official", "official_and_news"}
        if ticker and is_official and ticker_counts.get(ticker, 0) < min(2, per_ticker_limit):
            selected.append(note)
            ticker_counts[ticker] = ticker_counts.get(ticker, 0) + 1
            if len(selected) >= preselection_limit:
                break

    # 保证每个有候选的 ticker 至少一条。
    for note in notes:
        if note in selected or len(selected) >= preselection_limit:
            continue
        ticker = str(note.get("ticker") or "")
        if ticker and ticker_counts.get(ticker, 0) == 0:
            selected.append(note)
            ticker_counts[ticker] = 1

    # 再按分数填充；完整管线可扩大配额，直接调用保持旧版每 ticker 3 条。
    for note in notes:
        if note in selected or len(selected) >= preselection_limit:
            continue
        ticker = str(note.get("ticker") or "")
        if ticker:
            if ticker_counts.get(ticker, 0) >= per_ticker_limit:
                continue
            ticker_counts[ticker] = ticker_counts.get(ticker, 0) + 1
        else:
            if macro_count >= 4:
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
    # 第七轮第 3 节：最终 evidence_id 不再在此处分配，改由收口点 finalize_evidence_ids 统一编号。
    # 预标记 snippet_fallback_ok，供收口点判断是否可作为未验证但可用的证据。
    for note in selected:
        if not note.get("article_fetch_ok"):
            note["snippet_fallback_ok"] = _dated_snippet_fallback_ok(note)
    selected.sort(key=lambda n: n.get("priority_score", 0), reverse=True)
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

    # ── 第六轮入口：基于 AI Research Plan 的多通道搜索 ─────
    def research_plan(
        self,
        *,
        top_risk_tickers: list[str],
        instrument_metadata: dict[str, dict[str, Any]],
        snapshot: dict[str, Any],
        metrics: dict[str, Any],
        ranking: dict[str, Any],
        model: str,
        provider: str,
        benchmark: str = "^GSPC",
        previous_events: list[dict[str, Any]] | None = None,
        save_plan_path: "os.PathLike[str] | str | None" = None,
        max_results_per_query: int = 3,
        max_articles: int = 15,
        max_evidence: int = 15,
    ) -> dict[str, Any]:
        """第六轮主入口：AI Planner -> Validator -> Compiler -> 多通道搜索。

        返回结构与 ``research()`` 兼容，额外在 ``diagnostics`` 中携带 planner
        信息（planner_mode / planner_model / planner_provider / planner_errors /
        planner_fallback_reason）。
        """
        plan, planner_diag = build_ai_research_plan(
            top_risk_tickers=top_risk_tickers,
            snapshot=snapshot,
            metrics=metrics,
            ranking=ranking,
            instrument_metadata=instrument_metadata,
            previous_events=previous_events,
            model=model,
            provider=provider,
            benchmark=benchmark,
            save_path=save_plan_path,
        )
        compiled_queries = compile_research_queries(
            plan, instrument_metadata=instrument_metadata, benchmark=benchmark,
        )
        official_queries = expand_official_lane_queries(
            compiled_queries, instrument_metadata=instrument_metadata,
        )
        all_queries = compiled_queries + official_queries

        raw: list[dict[str, Any]] = []
        query_stats: list[dict[str, Any]] = []
        provider_errors: list[str] = []
        provider_used = None
        verticals_used: set[str] = set()
        from .research_core.source_lanes import should_use_news_vertical
        for q in all_queries:
            lane = str(q.get("lane") or "news")
            use_news = should_use_news_vertical(lane)
            search_result = self._service.search(
                [q["query"]],
                provider=self.provider,
                max_results=max_results_per_query,
                recency_days=int(q.get("lookback_days") or 30),
                use_news_vertical=use_news,
            )
            results = list(search_result.get("results") or [])
            retrieved_at = datetime.now().astimezone().isoformat()
            diag = search_result.get("diagnostics") or {}
            provider_used = provider_used or diag.get("provider_used")
            provider_errors.extend(diag.get("errors") or [])
            for v in (diag.get("verticals_used") or []):
                verticals_used.add(v)
            for r in results:
                r.setdefault("search_retrieved_at", retrieved_at)
                r.setdefault("scope", q.get("scope") or "ticker")
                r.setdefault("ticker", q.get("ticker"))
                r.setdefault("event_hint", q.get("event_need"))
                r.setdefault("lane", q.get("lane"))
                r.setdefault("vertical", r.get("vertical") or ("news" if use_news else "search"))
                r.setdefault("question_id", q.get("question_id"))
                r.setdefault("preferred_domains", q.get("preferred_domains") or [])
                r.setdefault("required_entities", q.get("required_entities") or [])
                r.setdefault("exclude_terms", q.get("exclude_terms") or [])
            raw.extend(results)
            query_stats.append({
                "ticker": q.get("ticker"),
                "scope": q.get("scope"),
                "query": q.get("query"),
                "raw_query": q.get("raw_query"),
                "lane": q.get("lane"),
                "event_need": q.get("event_need"),
                "question_id": q.get("question_id"),
                "language": q.get("language"),
                "lookback_days": q.get("lookback_days"),
                "use_news_vertical": use_news,
                "raw_count": len(results),
                "accepted_count": 0,
                "rejected": {},
            })

        # 第七轮第 5 节：Gap Analyzer 只产出 query；补搜结果合并进统一候选池，
        # 由 process_all_results_once 单一收口，杜绝子流程自行编号导致的重复 ID。
        gap_diagnostics = analyze_research_gap(
            plan, raw, model=model, provider=provider,
            instrument_metadata=instrument_metadata, first_pass=True,
        )
        initial_gap_budget = max(1, MAX_GAP_QUERIES // 2)
        initial_gap_queries = (gap_diagnostics.get("gap_queries") or [])[:initial_gap_budget]
        for gq in initial_gap_queries:
            qtext = str(gq.get("query") or "")
            if not qtext:
                continue
            try:
                gap_lane = str(gq.get("lane") or "official_and_news")
                gap_use_news = gap_lane in {"news", "official_and_news"}
                search_result = self._service.search(
                    [qtext], provider=self.provider, max_results=max_results_per_query,
                    recency_days=int(gq.get("lookback_days") or 30), use_news_vertical=gap_use_news,
                )
                results = list(search_result.get("results") or [])
                retrieved_at = datetime.now().astimezone().isoformat()
                for r in results:
                    r.setdefault("search_retrieved_at", retrieved_at)
                    r.setdefault("scope", gq.get("scope") or "ticker")
                    r.setdefault("ticker", gq.get("ticker"))
                    r.setdefault("event_hint", gq.get("event_need"))
                    r.setdefault("lane", gq.get("lane") or "news")
                    r.setdefault("question_id", gq.get("question_id"))
                    r.setdefault("gap_search", True)
                    r.setdefault("gap_stage", "first_pass")
                raw.extend(results)
                query_stats.append({
                    "ticker": gq.get("ticker"), "scope": "ticker",
                    "query": qtext, "lane": gap_lane,
                    "event_need": gq.get("event_need"), "lookback_days": gq.get("lookback_days"),
                    "use_news_vertical": gap_use_news, "raw_count": len(results),
                    "accepted_count": 0, "rejected": {}, "gap_stage": "first_pass",
                })
            except Exception as exc:  # noqa: BLE001
                provider_errors.append(f"first_pass_gap_search_failed:{type(exc).__name__}:{exc}")

        # 初始+第一轮补搜统一进入同一管线。
        processed = process_all_results_once(
            raw,
            plan=plan,
            instrument_metadata=instrument_metadata,
            ranking=ranking,
            metrics=metrics,
            max_articles=max_articles,
            max_evidence=max_evidence,
            search_provider=self.provider,
        )
        selected = processed["evidence"]
        materiality_stats = processed["materiality_stats"]
        event_clusters = processed["event_clusters"]
        reference_count = processed["reference_count"]
        outside_window_count = processed["outside_window_count"]
        filtered_count = processed["filtered_count"]

        # 第二级 Gap Gate：Raw Candidate 覆盖不等于 Materiality 覆盖。
        # 第一轮管线完成后再按 materiality_accepted 检查一次，使用剩余 Query 预算精准补搜。
        post_gap_diagnostics = analyze_research_gap(
            plan, selected, model=model, provider=provider,
            instrument_metadata=instrument_metadata, first_pass=False,
        )
        remaining_gap_budget = max(0, MAX_GAP_QUERIES - len(initial_gap_queries))
        post_gap_queries = (post_gap_diagnostics.get("gap_queries") or [])[:remaining_gap_budget]
        if post_gap_queries:
            for gq in post_gap_queries:
                qtext = str(gq.get("query") or "")
                if not qtext:
                    continue
                try:
                    search_result = self._service.search(
                        [qtext], provider=self.provider, max_results=max_results_per_query,
                        recency_days=int(gq.get("lookback_days") or 45), use_news_vertical=False,
                    )
                    results = list(search_result.get("results") or [])
                    retrieved_at = datetime.now().astimezone().isoformat()
                    for result in results:
                        result.setdefault("search_retrieved_at", retrieved_at)
                        result.setdefault("scope", "ticker")
                        result.setdefault("ticker", gq.get("ticker"))
                        result.setdefault("event_hint", gq.get("event_need"))
                        result.setdefault("lane", gq.get("lane") or "official_and_news")
                        result.setdefault("question_id", gq.get("question_id"))
                        result.setdefault("gap_search", True)
                        result.setdefault("gap_stage", "post_materiality")
                    raw.extend(results)
                    query_stats.append({
                        "ticker": gq.get("ticker"), "scope": "ticker",
                        "query": qtext, "lane": gq.get("lane") or "official_and_news",
                        "event_need": gq.get("event_need"), "lookback_days": gq.get("lookback_days"),
                        "use_news_vertical": False, "raw_count": len(results),
                        "accepted_count": 0, "rejected": {}, "gap_stage": "post_materiality",
                    })
                except Exception as exc:  # noqa: BLE001
                    provider_errors.append(f"post_materiality_gap_search_failed:{type(exc).__name__}:{exc}")

            # 新结果与原始结果再次统一收口一次，不产生子流程 Evidence ID。
            processed = process_all_results_once(
                raw, plan=plan, instrument_metadata=instrument_metadata,
                ranking=ranking, metrics=metrics, max_articles=max_articles,
                max_evidence=max_evidence, search_provider=self.provider,
            )
            selected = processed["evidence"]
            materiality_stats = processed["materiality_stats"]
            event_clusters = processed["event_clusters"]
            reference_count = processed["reference_count"]
            outside_window_count = processed["outside_window_count"]
            filtered_count = processed["filtered_count"]

        # Risk-weighted coverage（修改计划第 21 节）
        risk_weighted_coverage = _risk_weighted_coverage(
            top_risk_tickers, selected, ranking, metrics,
        )

        unresolved_date_count = sum(1 for item in selected if item.get("recency_tier") == "unknown")
        unverified_count = sum(1 for item in selected if not item.get("article_fetch_ok"))
        snippet_fallback_count = sum(
            1 for item in selected if not item.get("article_fetch_ok") and item.get("snippet_fallback_ok")
        )
        snippet_too_weak_count = sum(
            1 for item in selected if not item.get("article_fetch_ok") and not item.get("snippet_fallback_ok")
        )
        verified_article_count = sum(1 for item in selected if item.get("article_fetch_ok"))
        materiality_accepted_count = sum(1 for item in selected if item.get("materiality_accepted"))

        raw_count = len(raw)
        # 返回 evidence 为含 materiality 评分的全量候选（uid 已分配，evidence_id 待收口点分配）。
        # 最终 accepted 分流由 run_portfolio_report 在 summarize + finalize 后完成。
        evidence = selected
        covered = {str(e.get("ticker")) for e in selected if e.get("ticker") and e.get("materiality_accepted")}
        coverage = len(set(top_risk_tickers) & covered) / len(top_risk_tickers) if top_risk_tickers else 1.0
        if not raw_count:
            status = "not_configured" if any("not configured" in e for e in provider_errors) else ("provider_error" if provider_errors else "no_raw_results")
        elif not filtered_count:
            status = "all_filtered"
        elif not evidence:
            status = "all_filtered"
        elif coverage < float(os.environ.get("PORTFOLIO_REPORT_MIN_NEWS_COVERAGE", "0.60")):
            status = "insufficient_coverage"
        else:
            status = "success"

        # 新闻检索执行时间（修改计划第 27 节）
        news_search_executed_at = datetime.now().astimezone().isoformat()
        latest_event_date = None
        for item in evidence:
            d = str(item.get("published_date") or "")
            if d and len(d) >= 10:
                if latest_event_date is None or d > latest_event_date:
                    latest_event_date = d[:10]

        diagnostics = {
            "status": status,
            "provider_requested": self.provider,
            "provider_used": provider_used,
            "planner_mode": planner_diag.get("planner_mode"),
            "planner_model": planner_diag.get("planner_model"),
            "planner_provider": planner_diag.get("planner_provider"),
            "planner_enabled": planner_diag.get("planner_enabled"),
            "planner_temperature": planner_diag.get("planner_temperature"),
            "planner_errors": planner_diag.get("planner_errors") or [],
            "planner_fallback_reason": planner_diag.get("planner_fallback_reason"),
            "plan_version": plan.get("plan_version"),
            "plan_total_queries": plan.get("total_queries"),
            "compiled_queries_count": len(compiled_queries),
            "official_lane_queries_count": len(official_queries),
            "total_executed_queries": len(all_queries) + len(initial_gap_queries) + len(post_gap_queries),
            "queries_count": len(all_queries) + len(initial_gap_queries) + len(post_gap_queries),
            "verticals_used": sorted(verticals_used),
            "raw_results_count": raw_count,
            "filtered_results_count": filtered_count,
            "selected_evidence_count": len(evidence),
            "materiality_accepted_count": materiality_accepted_count,
            "verified_article_count": verified_article_count,
            "top_risk_coverage": round(coverage, 3),
            "risk_weighted_coverage": round(risk_weighted_coverage, 3),
            "news_search_executed_at": news_search_executed_at,
            "latest_selected_event_date": latest_event_date,
            "errors": list(dict.fromkeys(provider_errors)),
            "rejected": {
                **processed["filter_rejected"],
                "reference_page": reference_count,
                "unknown_date": unresolved_date_count,
                "outside_recent_window": outside_window_count,
                "article_unverified": unverified_count,
                "snippet_too_weak": snippet_too_weak_count,
            },
            "snippet_fallback_count": snippet_fallback_count,
            "strict_article_verification": _REQUIRE_VERIFIED_ARTICLE,
            "query_stats": query_stats,
            "search_lanes": _search_lanes_summary(
                all_queries + initial_gap_queries + post_gap_queries
            ),
            "materiality_stats": materiality_stats,
            "event_clusters": event_clusters,
            "event_cluster_count": len(event_clusters),
            "gap_mode": gap_diagnostics.get("gap_mode"),
            "gap_additional_search_required": bool(initial_gap_queries or post_gap_queries),
            "gap_total_new_queries": len(initial_gap_queries) + len(post_gap_queries),
            "gap_errors": (gap_diagnostics.get("errors") or []) + (post_gap_diagnostics.get("errors") or []),
            "gap_diagnostics": {
                "first_pass": gap_diagnostics,
                "post_materiality": post_gap_diagnostics,
            },
            "first_pass_gap_query_count": len(initial_gap_queries),
            "post_materiality_gap_query_count": len(post_gap_queries),
        }
        return {
            "status": status,
            "evidence": evidence,
            "diagnostics": diagnostics,
            "raw_results": raw,
            "filtered_results": processed["filtered"],
            "research_plan": plan,
        }

    def precision_gap_search(
        self,
        *,
        plan: dict[str, Any],
        accepted_evidence: list[dict[str, Any]],
        instrument_metadata: dict[str, dict[str, Any]],
        ranking: dict[str, Any],
        metrics: dict[str, Any],
        model: str,
        provider: str,
        max_results_per_query: int = 4,
        max_articles: int = 12,
        max_evidence: int = 15,
    ) -> dict[str, Any]:
        """Accepted Evidence 收口后的最后一次精准补搜。

        该方法不分配 ``evidence_id``，返回的新候选仍须经过 Decision Summarizer
        和全局 ``finalize_evidence_ids``。
        """
        diagnostics = analyze_research_gap(
            plan, accepted_evidence, model=model, provider=provider,
            instrument_metadata=instrument_metadata, first_pass=False,
        )
        queries = (diagnostics.get("gap_queries") or [])[:MAX_GAP_QUERIES]
        raw: list[dict[str, Any]] = []
        errors: list[str] = []
        for query in queries:
            text = str(query.get("query") or "")
            if not text:
                continue
            try:
                result = self._service.search(
                    [text], provider=self.provider, max_results=max_results_per_query,
                    recency_days=int(query.get("lookback_days") or 45),
                    use_news_vertical=False,
                )
                rows = list(result.get("results") or [])
                retrieved_at = datetime.now().astimezone().isoformat()
                for row in rows:
                    row.setdefault("search_retrieved_at", retrieved_at)
                    row.setdefault("scope", "ticker")
                    row.setdefault("ticker", query.get("ticker"))
                    row.setdefault("event_hint", query.get("event_need"))
                    row.setdefault("lane", "official_and_news")
                    row.setdefault("gap_search", True)
                    row.setdefault("gap_stage", "post_accepted")
                raw.extend(rows)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"post_accepted_gap_search_failed:{type(exc).__name__}:{exc}")

        processed = process_all_results_once(
            raw, plan=plan, instrument_metadata=instrument_metadata, ranking=ranking,
            metrics=metrics, max_articles=max_articles, max_evidence=max_evidence,
            search_provider=self.provider,
        ) if raw else {
            "evidence": [], "filtered": [], "filter_rejected": {},
            "materiality_stats": {}, "event_clusters": [],
            "reference_count": 0, "outside_window_count": 0,
            "raw_count": 0, "filtered_count": 0,
        }
        diagnostics["errors"] = list(diagnostics.get("errors") or []) + errors
        diagnostics["executed_query_count"] = len(queries)
        diagnostics["raw_results_count"] = len(raw)
        diagnostics["selected_evidence_count"] = len(processed.get("evidence") or [])
        return {
            "evidence": processed.get("evidence") or [],
            "raw_results": raw,
            "filtered_results": processed.get("filtered") or [],
            "diagnostics": diagnostics,
        }

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
            retrieved_at = datetime.now().astimezone().isoformat()
            diag = search_result.get("diagnostics") or {}
            provider_used = provider_used or diag.get("provider_used")
            provider_errors.extend(diag.get("errors") or [])
            for r in results:
                r.setdefault("search_retrieved_at", retrieved_at)
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
            candidate_reference = (
                _coerce_reference_datetime(candidate.get("search_retrieved_at"))
                or datetime.now().astimezone()
            )
            tier = _recency_tier(
                candidate.get("published_date"), reference_datetime=candidate_reference,
            )
            if tier not in {"fresh_event", "recent_background", "unknown"}:
                outside_window_count += 1
                continue
            recent_filtered.append(candidate)
        selected = build_evidence_notes(
            recent_filtered, instrument_metadata,
            max_articles=max_articles, max_evidence=max_evidence,
            search_provider=self.provider,
        )
        # 第七轮第 3 节：legacy research() 路径无 AI Materiality 排序，
        # 注入 finalize_evidence_ids 所需最小字段（全部视为 materiality 通过）。
        for item in selected:
            item.setdefault("materiality_accepted", True)
            item.setdefault(
                "entity_role", "primary" if item.get("ticker") else "theme_primary",
            )
            item.setdefault("is_quote_page", False)
            item.setdefault("is_reference_page", item.get("evidence_kind") == "reference")
            item.setdefault("accept", True)
            item.setdefault("reject_reason", None)
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


# ── Risk-weighted Coverage（修改计划第 21 节）──────────────
def _risk_weighted_coverage(
    top_risk_tickers: list[str],
    evidence: list[dict[str, Any]],
    ranking: dict[str, Any],
    metrics: dict[str, Any],
) -> float:
    """计算 risk-weighted coverage。

    risk_weighted_coverage = sum(risk_contribution of covered top-risk tickers)
                             / sum(risk_contribution of all top-risk tickers)
    """
    if not top_risk_tickers:
        return 1.0
    rc_map = {item.get("ticker"): item for item in metrics.get("risk_contributions", []) or []}
    total_rc = sum(
        float((rc_map.get(t) or {}).get("risk_contribution") or 0.0)
        for t in top_risk_tickers
    )
    if total_rc <= 0:
        return 1.0 if evidence else 0.0
    covered = {str(e.get("ticker") or "").upper() for e in evidence if e.get("ticker")}
    covered_rc = sum(
        float((rc_map.get(t) or {}).get("risk_contribution") or 0.0)
        for t in top_risk_tickers
        if str(t).upper() in covered
    )
    return covered_rc / total_rc


def _search_lanes_summary(queries: list[dict[str, Any]]) -> dict[str, int]:
    """统计各搜索通道的 query 数量，供 HTML 诊断展示。"""
    summary: dict[str, int] = {"official": 0, "news": 0, "theme": 0, "macro": 0, "official_and_news": 0}
    for q in queries or []:
        lane = str(q.get("lane") or "news")
        summary[lane] = summary.get(lane, 0) + 1
    # official_and_news 既算 official 通道也算 news 通道
    summary["official_total"] = summary.get("official", 0) + summary.get("official_and_news", 0)
    summary["news_total"] = summary.get("news", 0) + summary.get("official_and_news", 0)
    return summary


# ── Phase 3: Materiality Ranking + Event Clustering（修改计划第 15-17 节）
def _apply_materiality_and_clustering(
    evidence: list[dict[str, Any]],
    instrument_metadata: dict[str, dict[str, Any]],
    ranking: dict[str, Any],
    metrics: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """对 build_evidence_notes 输出应用 materiality ranking 和 event clustering。

    返回 (ranked_evidence, materiality_stats, event_clusters)：
    - ranked_evidence：每条 evidence 增加 materiality 评分字段；硬过滤的保留但标记
      accepted=False（由上层决定是否剔除）。
    - materiality_stats：统计 rejected 原因分布。
    - event_clusters：聚类元信息（同事件去重）。
    """
    from .research_core.materiality_ranker import rank_evidence
    from .research_core.event_clusterer import annotate_event_identity, cluster_events

    if not evidence:
        return [], {"rejected_reasons": {}, "ranked_count": 0, "accepted_count": 0}, []

    # Event identity 必须先于 Novelty Score，避免 event_key 缺失时全部得到默认分。
    evidence = annotate_event_identity(evidence, instrument_metadata=instrument_metadata)
    ranked: list[dict[str, Any]] = []
    seen_event_keys: set[str] = set()
    reject_reasons: dict[str, int] = {}
    rejected_by_ticker: dict[str, int] = {}
    accepted_by_ticker: dict[str, int] = {}
    source_type_counts: dict[str, dict[str, int]] = {}
    lane_counts: dict[str, dict[str, int]] = {}

    for ev in evidence:
        ticker = ev.get("ticker")
        meta = instrument_metadata.get(ticker, {}) if ticker else {}
        # 用 article 正文（若已抓取）或 summary 作为 body
        body = ""
        if ev.get("article_fetch_ok") and ev.get("facts"):
            body = " ".join(str(f) for f in ev.get("facts") or [])
        elif ev.get("summary_zh"):
            body = str(ev.get("summary_zh"))
        else:
            body = str(ev.get("title") or "") + " " + str(ev.get("summary") or "")

        scores = rank_evidence(
            ev, ticker=ticker, meta=meta, ranking=ranking, metrics=metrics,
            body=body, seen_event_keys=seen_event_keys,
            event_key=ev.get("event_key"),
        )
        # 把评分字段合并到 evidence
        ev["primary_entity_score"] = scores["primary_entity_score"]
        ev["materiality_score"] = scores["materiality_score"]
        ev["recency_score"] = scores["recency_score"]
        ev["portfolio_impact_score"] = scores["portfolio_impact_score"]
        ev["decision_usefulness_score"] = scores["decision_usefulness_score"]
        ev["source_authority_score"] = scores["source_authority_score"]
        ev["source_type"] = scores.get("source_type") or ev.get("source_type") or "unknown"
        ev["source_is_official"] = bool(scores.get("source_is_official"))
        ev["novelty_score"] = scores["novelty_score"]
        ev["selection_score"] = scores["selection_score"]
        ev["entity_role"] = scores["entity_role"]
        ev["page_classification"] = scores["page_classification"]
        ev["is_quote_page"] = scores["is_quote_page"]
        ev["is_reference_page"] = scores["is_reference_page"]
        ev["materiality_accepted"] = scores["accepted"]
        ev["reject_reason"] = scores["reject_reason"]
        ticker_key = str(ticker or "MACRO")
        source_key = str(ev.get("source_type") or "unknown")
        lane_key = str(ev.get("lane") or "unknown")
        outcome = "accepted" if scores["accepted"] else "rejected"
        source_type_counts.setdefault(source_key, {"accepted": 0, "rejected": 0})[outcome] += 1
        lane_counts.setdefault(lane_key, {"accepted": 0, "rejected": 0})[outcome] += 1
        if scores["reject_reason"]:
            reject_reasons[scores["reject_reason"]] = reject_reasons.get(scores["reject_reason"], 0) + 1
            rejected_by_ticker[ticker_key] = rejected_by_ticker.get(ticker_key, 0) + 1
        else:
            accepted_by_ticker[ticker_key] = accepted_by_ticker.get(ticker_key, 0) + 1
        # §17 修复：将已处理 event_key 加入集合，确保后续 evidence 的 Novelty Score 能判断重复
        ek = ev.get("event_key")
        if ek:
            seen_event_keys.add(str(ek))
        ranked.append(ev)

    # Event clustering：同事件去重
    clustered, event_clusters = cluster_events(ranked, instrument_metadata=instrument_metadata)

    stats = {
        "ranked_count": len(ranked),
        "accepted_count": sum(1 for e in ranked if e.get("materiality_accepted")),
        "rejected_count": sum(1 for e in ranked if not e.get("materiality_accepted")),
        "rejected_reasons": reject_reasons,
        "accepted_by_ticker": accepted_by_ticker,
        "rejected_by_ticker": rejected_by_ticker,
        "source_type_counts": source_type_counts,
        "lane_counts": lane_counts,
        "cluster_count": len(event_clusters),
        "avg_selection_score": round(
            sum(float(e.get("selection_score") or 0) for e in ranked) / len(ranked), 3
        ) if ranked else 0.0,
    }
    return clustered, stats, event_clusters


def _select_final_evidence(
    ranked: list[dict[str, Any]],
    max_evidence: int,
) -> list[dict[str, Any]]:
    """Materiality 后再执行最终 Top-K，优先 accepted 且保持 ticker 多样性。"""
    limit = max(1, int(max_evidence))
    ordered = sorted(
        ranked,
        key=lambda item: (
            bool(item.get("materiality_accepted")),
            float(item.get("selection_score") or 0.0),
            float(item.get("priority_score") or 0.0),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    represented: set[str] = set()
    for item in ordered:
        ticker = str(item.get("ticker") or "")
        if item.get("materiality_accepted") and ticker and ticker not in represented:
            selected.append(item)
            represented.add(ticker)
            if len(selected) >= limit:
                return selected
    for item in ordered:
        if item not in selected:
            selected.append(item)
            if len(selected) >= limit:
                break
    return selected


# ── Phase 5: 单一收口点（第七轮第 5 节）─────────────
def process_all_results_once(
    all_raw_results: list[dict[str, Any]],
    *,
    plan: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]],
    ranking: dict[str, Any],
    metrics: dict[str, Any],
    max_articles: int = 15,
    max_evidence: int = 15,
    search_provider: str = "auto",
    reference_datetime: datetime | None = None,
) -> dict[str, Any]:
    """第七轮第 5 节：研究管线唯一收口点。

    初始搜索与补搜的原始结果都先合并到这里，再统一经过：
        filter → build_evidence_notes(uid) → article fetch → materiality + clustering
    只此一处产生证据，杜绝子流程自行编号导致的重复 evidence_id 串线。
    """
    filtered, filter_rejected = filter_candidates_with_diagnostics(all_raw_results, instrument_metadata)

    recent_filtered: list[dict[str, Any]] = []
    reference_count = outside_window_count = 0
    for candidate in filtered:
        ticker = candidate.get("ticker")
        meta = instrument_metadata.get(ticker, {}) if ticker else {}
        if _evidence_kind(candidate, candidate.get("scope"), meta) == "reference":
            reference_count += 1
            continue
        candidate_reference = (
            _coerce_reference_datetime(candidate.get("search_retrieved_at"))
            or reference_datetime
            or datetime.now().astimezone()
        )
        tier = _recency_tier(
            candidate.get("published_date"), reference_datetime=candidate_reference,
        )
        if tier not in {"fresh_event", "recent_background", "unknown"}:
            outside_window_count += 1
            continue
        recent_filtered.append(candidate)

    selected = build_evidence_notes(
        recent_filtered, instrument_metadata,
        max_articles=max_articles, max_evidence=max_evidence,
        search_provider=search_provider,
        max_candidates_per_ticker=8, preselection_multiplier=3,
        reference_datetime=reference_datetime,
    )
    selected, materiality_stats, event_clusters = _apply_materiality_and_clustering(
        selected, instrument_metadata, ranking, metrics,
    )
    expanded_ranked_count = len(selected)
    selected = _select_final_evidence(selected, max_evidence)
    materiality_stats["expanded_ranked_count"] = expanded_ranked_count
    materiality_stats["final_selected_count"] = len(selected)

    # 复刻既有 snippet 置信度上限逻辑：未验证但可用的 snippet 证据置信度封顶 0.60。
    for item in selected:
        if not item.get("article_fetch_ok") and item.get("snippet_fallback_ok"):
            item["content_basis"] = "search_snippet_unverified"
            item["verification_status"] = "search_snippet_unverified"
            item["confidence"] = round(min(0.60, float(item.get("confidence") or 0.40)), 2)

    return {
        "evidence": selected,
        "filtered": recent_filtered,
        "filter_rejected": filter_rejected,
        "reference_count": reference_count,
        "outside_window_count": outside_window_count,
        "raw_count": len(all_raw_results),
        "filtered_count": len(recent_filtered),
        "materiality_stats": materiality_stats,
        "event_clusters": event_clusters,
    }
