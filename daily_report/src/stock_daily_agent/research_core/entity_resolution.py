# -*- coding: utf-8 -*-
"""Primary Entity Resolution（修改计划第六轮第 15 节）。

判断搜索结果的主要主体是否真的是该标的，而非顺带提及。

- Equity：至少满足一个（ticker/公司别名出现在标题 / 官方域名匹配 / 正文开头明确以
  公司为主要主体 / 正文多次围绕公司展开）。仅在正文顺带出现 → incidental。
- ETF/ETC：必须分类为 product_event / theme_event / constituent_event /
  quote_page / reference_page。Quote page 永远不能进入 Action Evidence。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .source_lanes import is_official_domain, classify_source


# ETF/ETC 页面分类关键词
_QUOTE_PAGE_HINTS = (
    "quote", "stock price", "historische kurse", "real-time quote",
    "price history", "chart", "performance chart", "live price",
)
_REFERENCE_PAGE_HINTS = (
    "factsheet", "fund overview", "product overview", "prospectus",
    "key facts", "fund details", "portfolio holdings", "index methodology",
    "etf profile", "fund profile",
)
_PRODUCT_EVENT_HINTS = (
    "launch", "closure", "liquidation", "name change", "stock split",
    "merger", "conversion", "fee change", "ter change", "distribution change",
)
_THEME_EVENT_HINTS = (
    "supply", "outage", "policy", "regulation", "approval", "ban",
    "shortage", "surplus", "demand", "production cut", "capacity",
)
_CONSTITUENT_EVENT_HINTS = (
    "rebalance", "reconstitution", "addition", "removal", "index change",
    "constituent", "weighting change",
)


def _text_low(*parts: Any) -> str:
    return " ".join(str(p or "") for p in parts).lower()


_GENERIC_THEME_TOKENS = {
    "index", "fund", "ucits", "swap", "acc", "etf", "etc", "energy",
    "equity", "equities", "market", "markets", "growth", "large", "cap",
    "policy", "supply", "demand", "price", "prices", "american", "global",
}


def _entity_aliases(ticker: str, name: str, extra_aliases: list[str] | None = None) -> list[str]:
    """生成 ticker/公司名的别名列表（小写）。"""
    aliases = [ticker.lower(), ticker.split(".", 1)[0].lower(), (name or "").lower()]
    aliases.extend(str(item).lower() for item in (extra_aliases or []) if str(item).strip())
    shortened = re.sub(
        r"\b(incorporated|inc\.?|corporation|corp\.?|plc|ltd\.?|limited|se|ag|class\s+[a-z])\b",
        " ", name or "", flags=re.I,
    )
    shortened = re.sub(r"[,\s]+", " ", shortened).strip()
    if shortened:
        aliases.append(shortened.lower())
    first = shortened.split()[0] if shortened else ""
    if len(first) >= 4:
        aliases.append(first.lower())
    return list(dict.fromkeys(a.strip() for a in aliases if a and a.strip()))


def _contains_alias(text: str, alias: str) -> bool:
    """Match aliases without allowing short tickers to hit arbitrary substrings."""
    haystack = str(text or "").lower()
    needle = str(alias or "").strip().lower()
    if not haystack or not needle:
        return False
    if re.fullmatch(r"[a-z0-9.-]{1,6}", needle):
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack


def _count_alias(text: str, alias: str) -> int:
    haystack = str(text or "").lower()
    needle = str(alias or "").strip().lower()
    if not haystack or not needle:
        return 0
    if re.fullmatch(r"[a-z0-9.-]{1,6}", needle):
        return len(re.findall(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack))
    return haystack.count(needle)


def _theme_aliases(meta: dict[str, Any]) -> list[str]:
    """Build distinctive ETF/index theme aliases for theme-event resolution."""
    raw_values: list[str] = []
    for key in ("underlying_index", "theme"):
        value = str(meta.get(key) or "").strip()
        if value:
            raw_values.append(value)
    raw_values.extend(str(item).strip() for item in (meta.get("key_drivers") or []) if str(item).strip())

    aliases: list[str] = []
    for value in raw_values:
        low = value.lower()
        # Keep the meaningful English side of bilingual labels.
        for part in re.split(r"[/|]", low):
            part = re.sub(r"\s+", " ", part).strip(" -")
            if len(part) >= 5:
                aliases.append(part)
        for token in re.findall(r"[a-z0-9-]{5,}", low):
            if token not in _GENERIC_THEME_TOKENS:
                aliases.append(token)
    return list(dict.fromkeys(aliases))


def _title_entity_match(title: str, aliases: list[str]) -> bool:
    """ticker/公司别名是否出现在标题中。"""
    return any(_contains_alias(title, alias) for alias in aliases)


def _body_mention_count(body: str, aliases: list[str]) -> int:
    """统计正文中公司别名出现次数（取最大值）。"""
    if not body:
        return 0
    return max((_count_alias(body, alias) for alias in aliases if alias), default=0)


def _body_leads_with_entity(body: str, aliases: list[str]) -> bool:
    """正文开头 300 字符是否明确以公司为主要主体。"""
    if not body:
        return False
    head = body[:300]
    return any(_contains_alias(head, alias) for alias in aliases)


def classify_etf_page(title: str, summary: str, body: str, url: str) -> str:
    """分类 ETF/ETC 页面类型。

    返回：product_event / theme_event / constituent_event / quote_page / reference_page
    """
    text = _text_low(title, summary, body, url)
    url_low = (url or "").lower()
    path = urlparse(url_low).path.rstrip("/").lower()

    # Quote page 优先识别（最该拒绝）
    if any(h in text for h in _QUOTE_PAGE_HINTS):
        # 但如果同时含明确事件词，则不算纯 quote page
        has_event = any(h in text for h in _PRODUCT_EVENT_HINTS + _THEME_EVENT_HINTS + _CONSTITUENT_EVENT_HINTS)
        if not has_event:
            return "quote_page"

    # Reference page
    if any(h in text for h in _REFERENCE_PAGE_HINTS):
        return "reference_page"
    if any(k in url_low for k in ("factsheet", "/portfolio/", "holdings", "prospectus", "overview")):
        return "reference_page"

    # Product event
    if any(h in text for h in _PRODUCT_EVENT_HINTS):
        return "product_event"
    # Theme event
    if any(h in text for h in _THEME_EVENT_HINTS):
        return "theme_event"
    # Constituent event
    if any(h in text for h in _CONSTITUENT_EVENT_HINTS):
        return "constituent_event"

    # 默认：如果 URL 路径像产品页且无事件词 → reference_page
    if path in {"", "/news"} or path.endswith("/news"):
        # 新闻列表页，但无明确事件 → 当作 theme_event（宽松）
        return "theme_event"
    return "reference_page"


def resolve_primary_entity(
    result: dict[str, Any],
    ticker: str | None,
    meta: dict[str, Any],
    *,
    body: str = "",
) -> dict[str, Any]:
    """判断 result 的主要主体是否为 ticker。

    返回结构（修改计划第 15 节）：
        {
            "entity_role": "primary" | "incidental",
            "primary_entity_score": 0.0-1.0,
            "matched_entities": ["Oracle", ...],
            "title_entity_match": bool,
            "domain_entity_match": bool,
            "body_mention_count": int,
            "page_classification": "event"|"product_event"|...|"quote_page"|"reference_page",
            "is_quote_page": bool,
            "is_reference_page": bool,
        }
    """
    if not ticker:
        # 宏观/主题证据：默认 primary（由 materiality 进一步过滤）
        return {
            "entity_role": "primary",
            "primary_entity_score": 0.75,
            "matched_entities": [],
            "title_entity_match": False,
            "domain_entity_match": False,
            "body_mention_count": 0,
            "page_classification": "macro",
            "is_quote_page": False,
            "is_reference_page": False,
        }

    name = str(meta.get("name") or ticker)
    itype = str(meta.get("instrument_type") or "UNKNOWN").upper()
    aliases = _entity_aliases(ticker, name, meta.get("entity_aliases") or [])
    theme_aliases = _theme_aliases(meta)
    title = str(result.get("title") or "")
    summary = str(result.get("summary") or "")
    url = str(result.get("url") or "")
    domain = urlparse(url).netloc.lower().removeprefix("www.") if url else ""

    title_match = _title_entity_match(title, aliases)
    body_count = _body_mention_count(body or summary, aliases)
    body_leads = _body_leads_with_entity(body or summary, aliases)
    theme_title_match = _title_entity_match(title, theme_aliases)
    theme_body_count = _body_mention_count(body or summary, theme_aliases)
    theme_body_leads = _body_leads_with_entity(body or summary, theme_aliases)

    official_domains = meta.get("official_domains") or []
    domain_match = is_official_domain(domain, official_domains) if official_domains else False
    src_class = classify_source(domain, official_domains=official_domains,
                                 regulator_domains=meta.get("regulator_domains") or [])
    domain_match = domain_match or src_class["is_official"]

    matched: list[str] = []
    for alias in aliases + theme_aliases:
        if _contains_alias(title, alias) or _contains_alias(body or summary, alias):
            matched.append(alias)

    # ETF/ETC 页面分类
    page_classification = "event"
    is_quote_page = False
    is_reference_page = False
    if itype in {"ETF", "ETC", "FUND", "INDEX"}:
        page_classification = classify_etf_page(title, summary, body, url)
        is_quote_page = page_classification == "quote_page"
        is_reference_page = page_classification == "reference_page"
    # 评分。Equity/product event 使用产品主体；ETF theme/constituent event
    # 允许用底层指数、主题和 key driver 证明“主要主体”。这避免把真正的铀供给、
    # Nasdaq-100 调整等驱动误判为对 ETF 的 incidental mention。
    score = 0.0
    if itype in {"ETF", "ETC", "FUND", "INDEX"} and page_classification in {
        "theme_event", "constituent_event",
    }:
        if theme_title_match:
            score += 0.55
        if theme_body_leads:
            score += 0.20
        if theme_body_count >= 2:
            score += 0.15
        elif theme_body_count >= 1:
            score += 0.08
        # Product/issuer confirmation remains useful but is not required for a
        # genuine theme event from a major publication.
        if title_match:
            score += 0.10
        if domain_match:
            score += 0.10
    else:
        if title_match:
            score += 0.45
        if domain_match:
            score += 0.30
        if body_leads:
            score += 0.15
        if body_count >= 3:
            score += 0.15
        elif body_count >= 1:
            score += 0.05
    score = min(1.0, score)

    # quote/reference page 强制降分
    if is_quote_page:
        score = min(score, 0.25)
    elif is_reference_page:
        score = min(score, 0.40)

    # incidental 判定：仅在正文顺带出现，且无标题/域名匹配
    entity_role = "primary"
    has_theme_primary = (
        page_classification in {"theme_event", "constituent_event"}
        and (theme_title_match or theme_body_leads or theme_body_count >= 2)
    )
    if not title_match and not domain_match and body_count <= 1 and not body_leads and not has_theme_primary:
        entity_role = "incidental"
        score = min(score, 0.30)

    return {
        "entity_role": entity_role,
        "primary_entity_score": round(score, 3),
        "matched_entities": matched[:5],
        "title_entity_match": title_match,
        "theme_title_match": theme_title_match,
        "domain_entity_match": domain_match,
        "body_mention_count": body_count,
        "theme_body_mention_count": theme_body_count,
        "page_classification": page_classification,
        "is_quote_page": is_quote_page,
        "is_reference_page": is_reference_page,
    }
