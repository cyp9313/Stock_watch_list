# -*- coding: utf-8 -*-
"""Source Quality 分类器（第七轮修改计划 §14）。

对每条 Evidence 做确定性 source_type 和 content_type 分类：
- source_type：official / regulator / rating_agency / major_media / specialty_media /
  broker_content / aggregator / community / unknown
- content_type：filing / press_release / news_report / opinion / forecast /
  comparison / quote / reference / unknown

Opinion 类内容不能支撑事实性 Action（add/trim/reduce/exit）。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


# ── source_type 分类 ────────────────────────────────────────

# 官方/监管/评级机构域名规则
_OFFICIAL_IR_PATH = re.compile(r"/(investors?|ir|investor-relations?|financials?|sec-filings?|results)/?", re.I)
_REGULATOR_DOMAINS = frozenset({
    "sec.gov", "edgar.sec.gov", "www.sec.gov",
    "esma.europa.eu", "eba.europa.eu", "eiopa.europa.eu",
    "fca.org.uk", "pbc.gov.cn", "csrc.gov.cn", "sse.com.cn",
    "szse.cn", "hkex.com.hk", "sgx.com",
    "cftc.gov", "finra.org", "occ.treas.gov", "fdic.gov",
})
_RATING_DOMAINS_PATTERNS = re.compile(
    r"(?:moodys\.com|spglobal\.com|fitchratings\.com|morningstar\.com/credit)",
    re.I,
)
_MAJOR_MEDIA_DOMAINS = frozenset({
    "reuters.com", "bloomberg.com", "wsj.com", "ft.com",
    "cnbc.com", "nytimes.com", "washingtonpost.com", "bbc.com",
    "economist.com", "barrons.com", "marketwatch.com",
    "investors.com", "seekingalpha.com", "finance.yahoo.com",
    "caixinglobal.com", "scmp.com", "nikkei.com",
})
_SPECIALTY_DOMAINS_PATTERNS = re.compile(
    r"(?:benzinga\.com|zacks\.com|fool\.com|stockanalysis\.com"
    r"|tipranks\.com|gurufocus\.com|simplywall\.st"
    r"|investopedia\.com|tradingview\.com|coindesk\.com"
    r"|cointelegraph\.com|theblock\.co"
    r"|oilprice\.com|mining\.com|world-nuclear-news\.org)",
    re.I,
)
_AGGREGATOR_DOMAINS = frozenset({
    "google.com", "news.google.com", "bing.com", "finance.google.com",
    "yahoo.com", "finance.yahoo.com",
})
_COMMUNITY_DOMAINS = frozenset({
    "reddit.com", "stocktwits.com", "twitter.com", "x.com",
})


def _extract_domain(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _classify_source_type(domain: str, url: str, meta: dict[str, Any]) -> str:
    """确定性 source_type 分类（计划 §14.1）。"""
    d = domain.lower()

    # 1. Regulator
    if d in _REGULATOR_DOMAINS:
        return "regulator"

    # 2. Rating Agency
    if _RATING_DOMAINS_PATTERNS.search(d):
        return "rating_agency"

    # 3. Official（公司 IR 域名或 meta 中的 official_domains）
    official_domains = [str(od).lower() for od in (meta.get("official_domains") or [])]
    if d in official_domains or _OFFICIAL_IR_PATH.search(url.lower()):
        return "official"

    # 4. Aggregator
    for ad in _AGGREGATOR_DOMAINS:
        if d == ad or d.endswith("." + ad):
            return "aggregator"

    # 5. Community
    if d in _COMMUNITY_DOMAINS:
        return "community"

    # 6. Major Media
    for md in _MAJOR_MEDIA_DOMAINS:
        if d == md or d.endswith("." + md):
            return "major_media"

    # 7. Specialty / Broker
    if _SPECIALTY_DOMAINS_PATTERNS.search(d):
        return "specialty_media"

    return "unknown"


def _classify_content_type(title: str, snippet: str, source_type: str) -> str:
    """确定性 content_type 分类（计划 §14.1）。"""
    text = f"{title} {snippet}".lower()

    # SEC/Regulatory filing
    if re.search(r"\b(form\s+[48]\-?k|s\-?[13]|10\-?[kq]|20\-?f|6\-?k|13[dg]|def\s?14a|proxy|prospectus|registration\s+statement)\b", text, re.I):
        return "filing"

    # Press Release
    pr_patterns = re.compile(r"\b(press\s*release|announces\s+|launches\s+|unveils?\s+|releases?\s+new|nouvelles?\s+officielles?|communiqu\u00e9|\u516c\u544a|\u53d1\u5e03)", re.I)
    if pr_patterns.search(text):
        return "press_release"

    # Opinion / Commentary
    opinion_words = re.compile(r"\b(opinion|viewpoint|commentary|analysis|editorial|column|why\s+\w+\s+is\s+|should\s+you\s+|is\s+it\s+time\s+to\s+|top\s+picks?|best\s+stocks?|buy\s+these|sell\s+these|bearish\s+view|bullish\s+view|\u89c2\u70b9|\u8bc4\u8bba)", re.I)
    if opinion_words.search(text):
        return "opinion"

    # Forecast / Estimate
    forecast_words = re.compile(r"\b(forecast|estimate|predict|projection|outlook\s+(?:20|fy)\d{2}|guidance|expected\s+(?:to|revenue|eps)|\u9884\u6d4b|\u9884\u671f|\u5c55\u671b)", re.I)
    if forecast_words.search(text) and source_type != "official":
        return "forecast"

    # Comparison
    if re.search(r"\b(vs\.?|versus|compared?\s+to|comparison|better\s+than|rival|\u5bf9\u6bd4|\u6bd4\u8f83)", text, re.I):
        return "comparison"

    # Quote page
    if re.search(r"\b(quote|stock\s+price|chart|ticker\s+overview|historical\s+prices?|market\s+data)\b", text, re.I) and not re.search(r"\b(news|earnings|results|deal|merger|acquisition)\b", text, re.I):
        return "quote"

    # Reference / Factsheet
    if re.search(r"\b(fact\s*sheet|product\s+overview|fund\s+profile|etf\s+details|holdings\s+list)\b", text, re.I):
        return "reference"

    return "news_report"


def classify_source_quality(
    evidence_item: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Classification d'une preuve (source_type + content_type).

    Retourne {"source_type": ..., "content_type": ...} à merger dans l'item.
    """
    if meta is None:
        meta = {}
    url = str(evidence_item.get("url") or "")
    domain = _extract_domain(url) or str(evidence_item.get("source_domain") or "")
    title = str(evidence_item.get("title") or "")
    snippet = str(evidence_item.get("summary") or evidence_item.get("raw_snippet") or "")

    source_type = _classify_source_type(domain, url, meta)
    content_type = _classify_content_type(title, snippet, source_type)

    return {"source_type": source_type, "content_type": content_type}
