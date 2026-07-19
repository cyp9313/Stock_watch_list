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
        "missing_url": 0, "duplicate": 0, "account_platform": 0,
        "quote_only": 0, "low_quality": 0, "entity_mismatch": 0,
        "excluded_term": 0,
    }
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
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
            reason = "duplicate"
        elif _looks_like_account_platform(title, url):
            reason = "account_platform"
        elif _looks_like_quote_or_overview(title, summary):
            reason = "quote_only"
        elif any(h in (title + " " + url).lower() for h in _LOW_QUALITY_HINTS):
            reason = "low_quality"
        # §11 修复：exclude_terms 匹配（在 entity check 之前）
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
            "event_type": c.get("event_hint") or "general",
            "evidence_kind": evidence_kind,
            "title": title,
            "raw_title": title,
            "raw_snippet": summary,
            "raw_url": url,
            "raw_published_date": published_date,
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
            diag = search_result.get("diagnostics") or {}
            provider_used = provider_used or diag.get("provider_used")
            provider_errors.extend(diag.get("errors") or [])
            for v in (diag.get("verticals_used") or []):
                verticals_used.add(v)
            for r in results:
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
        gap_diagnostics = analyze_research_gap(plan, raw, model=model, provider=provider)
        for gq in (gap_diagnostics.get("gap_queries") or [])[:MAX_GAP_QUERIES]:
            qtext = str(gq.get("query") or "")
            if not qtext:
                continue
            try:
                search_result = self._service.search(
                    [qtext], provider=self.provider, max_results=max_results_per_query,
                    recency_days=int(gq.get("lookback_days") or 30), use_news_vertical=True,
                )
                results = list(search_result.get("results") or [])
                for r in results:
                    r.setdefault("scope", gq.get("scope") or "ticker")
                    r.setdefault("ticker", gq.get("ticker"))
                    r.setdefault("event_hint", gq.get("event_need"))
                    r.setdefault("lane", gq.get("lane") or "news")
                    r.setdefault("question_id", gq.get("question_id"))
                    r.setdefault("gap_search", True)
                raw.extend(results)
            except Exception:  # noqa: BLE001
                continue

        # 第七轮第 5 节：单一收口点——初始+补搜原始结果统一进入同一管线。
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
            "total_executed_queries": len(all_queries),
            "queries_count": len(all_queries),
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
            "search_lanes": _search_lanes_summary(all_queries),
            "materiality_stats": materiality_stats,
            "event_clusters": event_clusters,
            "event_cluster_count": len(event_clusters),
            "gap_mode": gap_diagnostics.get("gap_mode"),
            "gap_additional_search_required": gap_diagnostics.get("additional_search_required"),
            "gap_total_new_queries": gap_diagnostics.get("total_new_queries"),
            "gap_errors": gap_diagnostics.get("errors") or [],
        }
        return {
            "status": status,
            "evidence": evidence,
            "diagnostics": diagnostics,
            "raw_results": raw,
            "filtered_results": processed["filtered"],
            "research_plan": plan,
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
    from .research_core.event_clusterer import cluster_events

    if not evidence:
        return [], {"rejected_reasons": {}, "ranked_count": 0, "accepted_count": 0}, []

    ranked: list[dict[str, Any]] = []
    seen_event_keys: set[str] = set()
    reject_reasons: dict[str, int] = {}

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
        ev["novelty_score"] = scores["novelty_score"]
        ev["selection_score"] = scores["selection_score"]
        ev["entity_role"] = scores["entity_role"]
        ev["page_classification"] = scores["page_classification"]
        ev["is_quote_page"] = scores["is_quote_page"]
        ev["is_reference_page"] = scores["is_reference_page"]
        ev["materiality_accepted"] = scores["accepted"]
        ev["reject_reason"] = scores["reject_reason"]
        if scores["reject_reason"]:
            reject_reasons[scores["reject_reason"]] = reject_reasons.get(scores["reject_reason"], 0) + 1
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
        "cluster_count": len(event_clusters),
        "avg_selection_score": round(
            sum(float(e.get("selection_score") or 0) for e in ranked) / len(ranked), 3
        ) if ranked else 0.0,
    }
    return clustered, stats, event_clusters


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
        tier = _recency_tier(candidate.get("published_date"))
        if tier not in {"fresh_event", "recent_background", "unknown"}:
            outside_window_count += 1
            continue
        recent_filtered.append(candidate)

    selected = build_evidence_notes(
        recent_filtered, instrument_metadata,
        max_articles=max_articles, max_evidence=max_evidence,
        search_provider=search_provider,
    )
    selected, materiality_stats, event_clusters = _apply_materiality_and_clustering(
        selected, instrument_metadata, ranking, metrics,
    )

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
