# -*- coding: utf-8 -*-
"""Portfolio 新闻研究管线（重构版）。

解决修改计划 2.3 / 2.4 / 11 / 12：
- instrument-aware 查询（股票 / ETF / ETC / 指数 / 加密资产差异化）；
- 绝不再把账户分组（Trade Republic / Trading212）当作行业搜索；
- 候选过滤、去重、来源分级（Tier1/2/3）；
- 复用现有 SSRF 防护的文章抓取；
- 产出结构化 Evidence Notes（含中文事件摘要、影响方向/范围）。
"""
from __future__ import annotations

import re
from typing import Any

from .research_service import ResearchService
from .article_fetcher import _fetch_article_text
from .tools import _source_domain, _source_quality_score, _evidence_grade


# ── 查询生成（修改计划 11）────────────────────────────────────
def build_instrument_aware_queries(
    top_risk_tickers: list[str],
    instrument_metadata: dict[str, dict[str, Any]],
    benchmark: str = "^GSPC",
) -> list[dict[str, Any]]:
    """返回 [(query, scope, ticker_or_None, event_hint), ...]。"""
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

    # 限制查询数量，控制成本
    return queries[:14]


# ── 过滤（修改计划 12.2）──────────────────────────────────────
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
    # 纯行情/概览页：包含 quote 关键词但缺乏实质事件词
    if any(h in text for h in _QUOTE_ONLY_HINTS):
        has_event = re.search(r"(earnings|revenue|guidance|downgrade|upgrade|lawsuit|recall|fda|merger|acquisition|dividend|guidance|miss|beat|cut|raise)", text)
        if not has_event:
            return True
    return False


def _is_relevant_to_ticker(result: dict[str, Any], ticker: str | None, meta: dict[str, Any]) -> bool:
    if not ticker:
        return True
    text = " ".join([
        str(result.get("title") or ""), str(result.get("summary") or ""),
        str(result.get("url") or ""), str(result.get("query") or ""),
    ]).lower()
    name = str(meta.get("name") or ticker).lower()
    # ticker 或产品名应出现在结果中（欧洲挂牌需同时匹配 ticker 与产品名）
    if ticker.lower() in text:
        return True
    # 对 ETF/指数，底层指数或主题命中也可接受
    underlying = str(meta.get("underlying_index") or "").lower()
    theme = str(meta.get("theme") or "").lower()
    if underlying and underlying in text:
        return True
    if theme and theme.lower() in text:
        return True
    # 股票要求公司名出现，否则视为错配
    if name and name not in text:
        return False
    return True


def filter_candidates(
    candidates: list[dict[str, Any]],
    instrument_metadata: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in candidates:
        url = str(c.get("url") or "").strip()
        title = str(c.get("title") or "").strip()
        summary = str(c.get("summary") or "")
        ticker = c.get("ticker")
        meta = instrument_metadata.get(ticker, {}) if ticker else {}

        if not url or url.lower().startswith("javascript:"):
            continue
        if url in seen_urls or title in seen_titles:
            continue
        if _looks_like_account_platform(title, url):
            continue
        if _looks_like_quote_or_overview(title, summary):
            continue
        if any(h in (title + " " + url).lower() for h in _LOW_QUALITY_HINTS):
            continue
        if ticker and not _is_relevant_to_ticker(c, ticker, meta):
            continue
        # 去重记账
        seen_urls.add(url)
        seen_titles.add(title)
        out.append(c)
    return out


# ── 影响方向 / 范围推断 ───────────────────────────────────────
_NEG = ["downgrade", "miss", "cut", "lower", "weak", "loss", "lawsuit", "probe", "recall", "decline", "drop", "fall", "risk", "warning", "default", "下调", "降级", "亏损", "诉讼", "风险"]
_POS = ["upgrade", "beat", "raise", "higher", "strong", "profit", "growth", "record", "approval", "win", "gain", "outperform", "上调", "超预期", "盈利", "增长"]
_LONG = ["long-term", "outlook", "secular", "structural", "2026", "2027", "长期"]
_SHORT = ["today", "q2", "q3", "earnings", "immediate", "短期"]


def _infer_impact(text: str) -> tuple[str, str]:
    low = (text or "").lower()
    neg = sum(1 for w in _NEG if w in low)
    pos = sum(1 for w in _POS if w in low)
    direction = "neutral"
    if pos > neg:
        direction = "positive"
    elif neg > pos:
        direction = "negative"
    horizon = "short_term"
    if any(w in low for w in _LONG):
        horizon = "long_term"
    elif any(w in low for w in _SHORT):
        horizon = "short_term"
    return direction, horizon


def _summarize_zh(facts: list[str], title: str, source_name: str, published_date: str) -> str:
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


# ── 文章抓取 + Evidence Note 构建 ─────────────────────────────
def _tier_from_grade(grade: str) -> str:
    return {"A": "tier_1", "B": "tier_2", "C": "tier_3", "D": "tier_3", "TECH": "tier_2"}.get(grade, "tier_3")


def build_evidence_notes(
    candidates: list[dict[str, Any]],
    instrument_metadata: dict[str, dict[str, Any]],
    *,
    max_articles: int = 12,
    fetch_timeout: float = 12,
) -> list[dict[str, Any]]:
    """把候选结果转换为结构化 Evidence Notes（含中文摘要、影响方向）。"""
    notes: list[dict[str, Any]] = []
    article_slots = 0
    for c in candidates:
        ticker = c.get("ticker")
        meta = instrument_metadata.get(ticker, {}) if ticker else {}
        url = str(c.get("url") or "").strip()
        title = str(c.get("title") or "")
        source_name = str(c.get("source") or _source_domain(url) or "unknown")
        published_date = str(c.get("published_date") or "")
        summary = str(c.get("summary") or "")
        facts_raw = re.split(r"(?<=[.!?])\s+", summary)
        facts = [f.strip() for f in facts_raw if len(f.strip()) > 12][:4]
        score = _source_quality_score({"url": url, "title": title, "facts": summary, "source_date": published_date})
        grade = _evidence_grade({"url": url, "title": title, "facts": summary, "source_date": published_date, "source_quality_score": score})
        direction, horizon = _infer_impact(title + " " + summary)

        note = {
            "evidence_id": "",  # 稍后统一编号
            "scope": c.get("scope") or ("ticker" if ticker else "portfolio"),
            "ticker": ticker,
            "related_tickers": [ticker] if ticker else [],
            "event_type": c.get("event_hint") or "general",
            "title": title,
            "source_name": source_name,
            "source_domain": _source_domain(url),
            "published_date": published_date,
            "url": url,
            "source_quality": _tier_from_grade(grade),
            "source_quality_score": score,
            "facts": facts,
            "summary_zh": _summarize_zh(facts, title, source_name, published_date),
            "impact_direction": direction,
            "impact_horizon": horizon,
            "portfolio_relevance": _relevance_note(ticker, meta),
            "confidence": round(min(0.95, max(0.4, score / 100.0)), 2),
            "article_fetch_ok": False,
        }

        # 对高质量且与 top-risk 相关的来源尝试抓取正文
        if url and article_slots < max_articles and (score >= 60 or grade in {"A", "B"}):
            try:
                art = _fetch_article_text(url, timeout=fetch_timeout, max_chars=4000)
                if art.get("ok"):
                    note["article_fetch_ok"] = True
                    art_text = str(art.get("text") or "")
                    if len(art_text) > 40:
                        facts2 = [s.strip() for s in re.split(r"(?<=[.!?])\s+", art_text) if len(s.strip()) > 30][:4]
                        if facts2:
                            note["facts"] = facts2
                        note["summary_zh"] = _summarize_zh(note["facts"], title, source_name, published_date)
                        d2, h2 = _infer_impact(title + " " + art_text)
                        note["impact_direction"] = d2
                        note["impact_horizon"] = h2
            except Exception:  # noqa: BLE001
                pass
            article_slots += 1

        notes.append(note)

    # 统一编号 + 按质量排序
    notes.sort(key=lambda n: n.get("source_quality_score", 0), reverse=True)
    for i, n in enumerate(notes, start=1):
        n["evidence_id"] = f"E{i:03d}"
    return notes


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
        max_articles: int = 12,
    ) -> list[dict[str, Any]]:
        queries = build_instrument_aware_queries(top_risk_tickers, instrument_metadata, benchmark=benchmark)
        raw = []
        for q in queries:
            results = self._service.search([q["query"]], provider=self.provider, max_results=max_results_per_query)
            for r in results:
                r.setdefault("scope", q["scope"])
                r.setdefault("ticker", q["ticker"])
                r.setdefault("event_hint", q["event_hint"])
            raw.extend(results)
        filtered = filter_candidates(raw, instrument_metadata)
        return build_evidence_notes(filtered, instrument_metadata, max_articles=max_articles)
