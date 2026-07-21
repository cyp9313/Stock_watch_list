# -*- coding: utf-8 -*-
"""Single-call DashScope web research for portfolio reports.

This module deliberately implements a *single* DashScope Generation call with
built-in web search.  It replaces the previous Serper/query-planner/gap-search/
summarizer/agent chain for portfolio reports.

Hard guarantees:
- no external search provider is called;
- no retry or gap search is performed;
- at most one DashScope web-search call is made per report;
- model evidence is accepted only when local entity/title/URL-hint matching
  resolves it to an actual DashScope ``search_info.search_results`` item;
- URL and title are taken only from that DashScope source; publication dates
  come from provider-owned metadata or a bounded local fetch of that same URL,
  never from free-form model output;
- fresh official or trusted material-event sources can be promoted by local
  deterministic rules even when the model omits or mis-binds them.
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .portfolio_schema import default_fallback_advice


class PortfolioSingleSearchUnavailable(RuntimeError):
    """The one allowed DashScope call could not be made."""


class PortfolioSingleSearchOutputError(RuntimeError):
    """DashScope returned a response that cannot be safely consumed."""


_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "ref", "ref_", "source", "campaign", "cmpid", "s_cid",
}
_ALLOWED_ACTIONS = {"add", "hold", "trim", "reduce", "exit", "watch"}
_ALLOWED_STANCES = {"balanced", "bullish", "cautious_bullish", "neutral", "cautious_bearish", "defensive", "observe"}
_REGULATOR_DOMAINS = {
    "sec.gov", "federalreserve.gov", "ecb.europa.eu", "esma.europa.eu",
    "bafin.de", "europa.eu", "gov.uk", "treasury.gov", "bls.gov", "bea.gov",
}
_TRUSTED_NEWS_DOMAINS = {
    "reuters.com", "bloomberg.com", "cnbc.com", "wsj.com", "ft.com",
    "apnews.com", "businesswire.com", "globenewswire.com", "prnewswire.com",
    "finance.yahoo.com", "marketwatch.com", "barrons.com",
    # DeepSeek's China-region built-in search frequently returns Chinese-language
    # finance/technology publishers even for US tickers.  These domains are
    # allowed only for locally verified, material-event *watch* evidence; they
    # never unlock an add/trim/exit action by themselves.
    "news.10jqka.com.cn", "stock.10jqka.com.cn", "yuanchuang.10jqka.com.cn",
    "finance.sina.com.cn", "www.cls.cn", "cls.cn", "www.yicai.com", "yicai.com",
    "www.caixin.com", "caixin.com", "www.ithome.com", "ithome.com",
    "www.36kr.com", "36kr.com", "www.stcn.com", "stcn.com",
    "www.cnstock.com", "cnstock.com",
}
_GENERIC_ENTITY_ALIASES = {
    "meta", "gold", "technology", "energy", "nuclear", "uranium",
    "oracle", "coin", "world", "global", "core", "growth",
}

_REFERENCE_PAGE_DOMAINS = {
    "stockpage.10jqka.com.cn",
    "gushitong.baidu.com",
    "guba.eastmoney.com",
    "gubaf10.eastmoney.com",
    "xueqiu.com",
}
_REFERENCE_TITLE_PATTERNS = (
    "股吧", "行情", "股价", "历史数据", "公司简介", "company profile",
    "stock quote", "quote page", "community", "discussion forum",
)
_MARKET_ACTIVITY_PATTERNS = (
    "成交额", "成交量", "换手率", "当日美股中排", "trading volume",
    "most active", "market activity",
)
_MATERIAL_EVENT_PATTERNS = (
    "财报", "业绩", "指引", "盈利预警", "监管", "批准", "调查", "诉讼",
    "融资", "增发", "回购", "并购", "收购", "出售", "重组", "裁员",
    "earnings", "results", "guidance", "filing", "regulatory", "approval",
    "investigation", "lawsuit", "financing", "offering", "buyback", "merger",
    "acquisition", "restructuring", "layoff",
    "launch", "launched", "partnership", "contract", "appointment",
    "appoints", "chief executive", "chief financial officer", "credit rating",
    "debt issuance", "strategic review", "dividend", "investor day",
    "production", "deliveries", "vehicle deliveries", "unit sales",
    "发布", "推出", "上线", "合作", "合同", "任命", "首席执行官", "首席财务官",
    "评级", "债券", "派息", "投资者日", "财季", "季度业绩", "交付", "产量", "销量",
)
_ARTICLE_PATH_HINTS = (
    "/article/", "/articles/", "/story/", "/stories/", "/news/", "/press-release/",
    "/press_releases/", "/release/", "/releases/", "/2024/", "/2025/", "/2026/",
    "/dy/article/", "/doc-", ".shtml", ".html", ".htm",
)
_INDEX_PATH_HINTS = (
    "/quote/", "/quotes/", "/stock/", "/stocks/", "/symbol/", "/ticker/",
    "/company/", "/companies/", "/profile/", "/community/", "/forum/", "/forums/",
    "/nftags/", "/tag/", "/tags/",
)

_LANDING_TITLE_EXACT = {
    "press releases", "press release", "sec filings", "sec filing",
    "investor relations", "newsroom", "company announcements", "announcements",
    "公司公告", "公告", "新闻稿", "投资者关系",
}
_LANDING_PATH_EXACT = {
    "/press", "/press-releases", "/press_releases", "/sec-filings",
    "/filings", "/newsroom", "/investor-relations", "/investors",
}


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict())
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        try:
            return _jsonable(vars(value))
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3:
            raw = "\n".join(lines[1:-1]).strip()
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _response_content_sources_usage(response: Any) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    output = _obj_get(response, "output", {}) or {}
    choices = _obj_get(output, "choices", []) or []
    content = ""
    if choices:
        message = _obj_get(choices[0], "message", {}) or {}
        content = str(_obj_get(message, "content", "") or "")

    search_info = _obj_get(output, "search_info", {}) or {}
    sources = _obj_get(search_info, "search_results", []) or []
    sources = [_jsonable(item) for item in sources if isinstance(_jsonable(item), dict)]

    usage = _jsonable(_obj_get(response, "usage", {}) or {})
    if not usage:
        usage = _jsonable(_obj_get(output, "usage", {}) or {})
    meta = {
        "request_id": str(_obj_get(response, "request_id", "") or ""),
        "code": str(_obj_get(response, "code", "") or ""),
        "message": str(_obj_get(response, "message", "") or ""),
        "usage": usage if isinstance(usage, dict) else {},
        "search_info": _jsonable(search_info) if isinstance(_jsonable(search_info), dict) else {},
    }
    return content, sources, meta


def _canonical_url(url: str) -> str:
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not host or parts.username or parts.password:
        return ""
    port = parts.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low.startswith("utm_") or low in _TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, value))
    query = urlencode(sorted(query_items))
    return urlunsplit((scheme, netloc, path, query, ""))


def _domain(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _parse_reference_datetime(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            parsed = dt.datetime.now().astimezone()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed


def _parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:  # milliseconds
                timestamp /= 1000.0
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        if not match:
            return None
        try:
            return dt.date.fromisoformat(match.group(1))
        except ValueError:
            return None


def _parse_source_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(?:\[)?(?:ref_)?(\d+)(?:\])?", text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _parse_date_with_reference(value: Any, reference_datetime: dt.datetime) -> dt.date | None:
    parsed = _parse_date(value)
    if parsed:
        return parsed
    text = str(value or "").strip()
    if not text:
        return None
    low = text.lower()

    # Numeric epoch values occasionally arrive as strings.
    if re.fullmatch(r"\d{10,13}", text):
        try:
            timestamp = float(text)
            if timestamp > 10_000_000_000:
                timestamp /= 1000.0
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            pass

    relative = re.search(r"\b(\d+)\s*(minute|minutes|hour|hours|day|days)\s+ago\b", low)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit.startswith("minute"):
            delta = dt.timedelta(minutes=amount)
        elif unit.startswith("hour"):
            delta = dt.timedelta(hours=amount)
        else:
            delta = dt.timedelta(days=amount)
        return (reference_datetime - delta).date()

    cn_relative = re.search(r"(\d+)\s*(分钟|小时|天)前", text)
    if cn_relative:
        amount = int(cn_relative.group(1))
        unit = cn_relative.group(2)
        if unit == "分钟":
            delta = dt.timedelta(minutes=amount)
        elif unit == "小时":
            delta = dt.timedelta(hours=amount)
        else:
            delta = dt.timedelta(days=amount)
        return (reference_datetime - delta).date()
    if "前天" in text:
        return (reference_datetime - dt.timedelta(days=2)).date()
    if "昨天" in text:
        return (reference_datetime - dt.timedelta(days=1)).date()
    if "今天" in text:
        return reference_datetime.date()

    cn = re.search(r"\b(\d{4})年(\d{1,2})月(\d{1,2})日\b", text)
    if cn:
        try:
            return dt.date(int(cn.group(1)), int(cn.group(2)), int(cn.group(3)))
        except ValueError:
            return None
    compact = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", text)
    if compact:
        try:
            return dt.date(int(compact.group(1)), int(compact.group(2)), int(compact.group(3)))
        except ValueError:
            return None
    slash = re.search(r"\b(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\b", text)
    if slash:
        try:
            return dt.date(int(slash.group(1)), int(slash.group(2)), int(slash.group(3)))
        except ValueError:
            return None

    month_names = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
        "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    month_first = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b",
        low,
    )
    if month_first:
        try:
            return dt.date(int(month_first.group(3)), month_names[month_first.group(1)], int(month_first.group(2)))
        except (ValueError, KeyError):
            return None
    day_first = re.search(
        r"\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"[,]?\s+(20\d{2})\b",
        low,
    )
    if day_first:
        try:
            return dt.date(int(day_first.group(3)), month_names[day_first.group(2)], int(day_first.group(1)))
        except (ValueError, KeyError):
            return None
    return None


def _iter_source_date_candidates(
    source: Any,
    *,
    depth: int = 0,
    date_context: bool = False,
) -> list[Any]:
    if depth > 4:
        return []
    candidates: list[Any] = []
    if isinstance(source, dict):
        for key, value in source.items():
            key_norm = re.sub(r"[^a-z]", "", str(key).lower())
            child_date_context = date_context or any(
                token in key_norm for token in ("date", "time", "publish", "created", "updated")
            )
            candidates.extend(
                _iter_source_date_candidates(
                    value,
                    depth=depth + 1,
                    date_context=child_date_context,
                )
            )
    elif isinstance(source, (list, tuple)):
        for value in source:
            candidates.extend(
                _iter_source_date_candidates(
                    value,
                    depth=depth + 1,
                    date_context=date_context,
                )
            )
    elif date_context and isinstance(source, (str, int, float, dt.date, dt.datetime)):
        candidates.append(source)
    return candidates


def _source_date(source: dict[str, Any], reference_datetime: dt.datetime) -> str:
    def usable(parsed: dt.date | None) -> str:
        # Provider results occasionally expose a scheduled event date or a date
        # scraped from an index page as though it were the publication date.
        # A report must never advertise a candidate event later than its own
        # search timestamp.
        if parsed is None or parsed > reference_datetime.date():
            return ""
        return parsed.isoformat()

    for key in (
        "date", "published_date", "published_time", "publish_time", "publishTime",
        "publishedAt", "publishDate", "pub_date", "pubDate", "source_date", "time",
        "created_at", "updated_at", "display_time", "datePublished",
    ):
        parsed = _parse_date_with_reference(source.get(key), reference_datetime)
        normalized = usable(parsed)
        if normalized:
            return normalized

    # Search plug-ins are not fully consistent across models.  Inspect nested
    # metadata and all source-owned strings, while still refusing model-generated
    # dates.  This recovers fields such as ``meta.publishTime`` and dates embedded
    # in article URLs/snippets.
    for value in _iter_source_date_candidates(source):
        parsed = _parse_date_with_reference(value, reference_datetime)
        normalized = usable(parsed)
        if normalized:
            return normalized
    for key in ("url", "link", "title", "name", "snippet", "content", "summary"):
        parsed = _parse_date_with_reference(source.get(key), reference_datetime)
        normalized = usable(parsed)
        if normalized:
            return normalized
    return ""


def _normalize_match_text(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"https?://", " ", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())


def _alias_in_text(alias: str, haystack: str) -> bool:
    """Match Latin ticker/name aliases next to either spaces or CJK text.

    ``\b`` is unsuitable for strings such as ``SoFi贷款`` because Python treats
    Chinese characters as word characters.  Restricting boundaries to ASCII
    letters/digits keeps ticker matching precise while allowing CJK adjacency.
    """
    normalized_alias = _normalize_match_text(alias)
    if not normalized_alias:
        return False
    # A CJK company name is commonly attached directly to dates/numbers, e.g.
    # 特斯拉7月 or 微软2026.  ASCII boundary rules would reject those valid
    # matches, so localised aliases use an exact normalized substring match.
    if re.search(r"[\u4e00-\u9fff]", normalized_alias):
        return normalized_alias in haystack
    pattern = rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def _research_aliases(ticker: str, meta: dict[str, Any]) -> list[str]:
    values: list[str] = [ticker, ticker.split(".", 1)[0], str(meta.get("name") or "")]
    for key in ("entity_aliases", "localized_aliases", "search_aliases"):
        aliases = meta.get(key) or []
        if isinstance(aliases, str):
            aliases = [aliases]
        values.extend(str(x or "") for x in aliases)
    out: list[str] = []
    for value in values:
        normalized = _normalize_match_text(value)
        if not normalized or normalized in _GENERIC_ENTITY_ALIASES:
            continue
        compact = normalized.replace(" ", "")
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", compact))
        # Localised company names such as 特斯拉/微软 are intentionally short.
        # Keep a stricter four-character floor only for Latin aliases, while
        # allowing precise CJK aliases of at least two characters.
        if (has_cjk and len(compact) < 2) or (not has_cjk and len(compact) < 4):
            continue
        if normalized not in out:
            out.append(normalized)
    return sorted(out, key=len, reverse=True)


def _build_research_targets(
    snapshot: dict[str, Any],
    ranking: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]],
    *,
    requested_limit: int,
    entity_limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Choose a compact, search-friendly subset for the single turbo call.

    One broad search over five unrelated companies, ETFs, and themes produced
    low-quality recall in real runs.  The single-search path therefore gives
    priority to direct-company equities and limits the web-search intent to at
    most three exact entities.  Quantitative analysis and the observation list
    still cover the full portfolio.
    """
    holdings = {
        str(item.get("ticker") or "").upper(): item
        for item in snapshot.get("holdings", [])
    }
    ranked = [str(x or "").upper() for x in ranking.get("top_risk_tickers") or []]
    candidates: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    for position, ticker in enumerate(ranked):
        holding = holdings.get(ticker, {})
        meta = instrument_metadata.get(ticker, {}) or {}
        instrument_type = str(
            meta.get("instrument_type")
            or holding.get("instrument_type")
            or holding.get("asset_type")
            or ""
        ).upper()
        direct_equity = instrument_type in {"EQUITY", "STOCK", "COMMON_STOCK"}
        # Unknown US-style symbols are usually direct equities; known ETF/ETC
        # overrides remain non-equity and are searched only as fallback.
        if not instrument_type and "." not in ticker and "-" not in ticker and "=" not in ticker:
            direct_equity = True
        priority_bucket = 0 if direct_equity else 1
        candidates.append((priority_bucket * 100 + position, ticker, holding, meta))
    candidates.sort(key=lambda x: x[0])
    target_limit = max(1, min(entity_limit, requested_limit))
    selected = candidates[:target_limit]
    targets: list[dict[str, Any]] = []
    for _, ticker, holding, meta in selected:
        aliases = _research_aliases(ticker, meta)
        official_domains = sorted(_official_domains(meta))
        targets.append({
            "ticker": ticker,
            "name": str(meta.get("name") or holding.get("name") or ticker),
            "aliases": aliases,
            "official_domains": official_domains,
            "instrument_type": str(meta.get("instrument_type") or holding.get("instrument_type") or ""),
            "theme": str(meta.get("theme") or meta.get("underlying_index") or ""),
        })
    selected_tickers = {x["ticker"] for x in targets}
    omitted = [ticker for ticker in ranked[:requested_limit] if ticker not in selected_tickers]
    return targets, omitted


def _annotate_source_relevance(
    records: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> None:
    target_map = {str(t.get("ticker") or "").upper(): t for t in targets}
    for record in records:
        domain = str(record.get("source_domain") or "")
        haystack = _normalize_match_text(
            " ".join([
                str(record.get("title") or ""),
                str(record.get("snippet") or ""),
                str(record.get("url") or ""),
            ])
        )
        matched: list[str] = []
        reasons: list[str] = []
        for ticker, target in target_map.items():
            official_domains = set(target.get("official_domains") or [])
            if official_domains and _domain_matches(domain, official_domains):
                matched.append(ticker)
                reasons.append(f"{ticker}:official_domain")
                continue
            aliases = target.get("aliases") or []
            alias_match = next((alias for alias in aliases if _alias_in_text(alias, haystack)), "")
            if alias_match:
                matched.append(ticker)
                reasons.append(f"{ticker}:alias={alias_match}")
        record["matched_tickers"] = matched
        record["is_relevant"] = len(matched) == 1
        record["relevance_status"] = (
            "relevant" if len(matched) == 1 else "ambiguous" if len(matched) > 1 else "irrelevant"
        )
        record["relevance_reason"] = ";".join(reasons) if reasons else "no_exact_entity_match"


def _official_domains(meta: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("official_domains", "preferred_domains"):
        value = meta.get(key)
        if isinstance(value, str):
            value = [value]
        for item in value or []:
            domain = str(item or "").lower().strip().lstrip(".")
            if domain:
                out.add(domain)
    return out


def _domain_matches(domain: str, candidates: set[str]) -> bool:
    return any(domain == candidate or domain.endswith("." + candidate) for candidate in candidates)


def _model_search_capabilities(model: str) -> dict[str, bool]:
    """Return only capabilities that Model Studio documents for the model.

    Third-party models such as DeepSeek can use built-in search and return the
    source list, but citation markers, provider-side freshness filtering and
    source-scope controls are not dependable there.  Keeping this explicit
    prevents the validator from assuming guarantees that the provider did not
    make.
    """
    name = str(model or "").lower().strip()
    qwen_search_model = name.startswith(("qwen-", "qwen3-", "qwq-"))
    return {
        "supports_citation": qwen_search_model,
        "supports_freshness": qwen_search_model,
        "supports_site_scope": qwen_search_model,
    }


def _source_quality(domain: str, ticker: str, instrument_metadata: dict[str, dict[str, Any]]) -> tuple[str, str]:
    if _domain_matches(domain, _REGULATOR_DOMAINS):
        return "tier_1", "regulator"
    official = _official_domains(instrument_metadata.get(ticker, {}) or {})
    if official and _domain_matches(domain, official):
        return "tier_1", "official"
    if _is_trusted_news_domain(domain):
        return "tier_2", "news"
    return "tier_2", "web_source"


def _is_trusted_news_domain(domain: str) -> bool:
    return _domain_matches(str(domain or "").lower(), _TRUSTED_NEWS_DOMAINS)


def _classify_source_for_reference(record: dict[str, Any]) -> dict[str, Any]:
    """Classify whether a DashScope source is safe to publish as background.

    A missing publication date no longer makes a genuine article disappear from
    the report.  Dated sources remain stronger; undated article pages are shown
    as non-decision background with an explicit date warning.  Quote pages,
    forums, profiles and landing pages remain diagnostic-only.
    """
    title = str(record.get("title") or "").strip()
    url = str(record.get("url") or "").strip()
    domain = str(record.get("source_domain") or _domain(url)).lower()
    published_date = str(record.get("published_date") or "").strip()
    normalized = _normalize_match_text(title)
    path = ""
    try:
        path = (urlsplit(url).path or "").lower()
    except ValueError:
        pass
    path_no_slash = path.rstrip("/") or "/"

    if not record.get("is_relevant"):
        return {"page_type": "irrelevant", "citable_as_reference": False, "reference_reject_reason": "not_exactly_one_entity"}
    if not title or title == url or title.lower().startswith(("http://", "https://")):
        return {"page_type": "missing_title", "citable_as_reference": False, "reference_reject_reason": "missing_article_title"}
    if domain in _REFERENCE_PAGE_DOMAINS:
        return {"page_type": "quote_or_community", "citable_as_reference": False, "reference_reject_reason": "quote_or_community_page"}
    if any(pattern in normalized for pattern in _REFERENCE_TITLE_PATTERNS):
        return {"page_type": "reference_page", "citable_as_reference": False, "reference_reject_reason": "reference_or_quote_title"}
    # Search providers often return an issuer's press/filing *index* instead of
    # an individual release.  These are useful navigation pages but not news
    # evidence and must never appear as Rxxx cards or supply a publication date.
    if normalized in _LANDING_TITLE_EXACT or path_no_slash in _LANDING_PATH_EXACT:
        return {"page_type": "official_landing", "citable_as_reference": False, "reference_reject_reason": "official_index_or_landing_page"}
    if path_no_slash.endswith("/pub.html") or path_no_slash.endswith("/pub"):
        return {"page_type": "announcement_index", "citable_as_reference": False, "reference_reject_reason": "announcement_index_page"}
    if path in {"", "/"} or path.rstrip("/").endswith("/news"):
        return {"page_type": "index_page", "citable_as_reference": False, "reference_reject_reason": "index_or_landing_page"}
    if any(hint in path for hint in _INDEX_PATH_HINTS) and not any(hint in path for hint in _ARTICLE_PATH_HINTS):
        return {"page_type": "reference_page", "citable_as_reference": False, "reference_reject_reason": "index_or_profile_path"}

    date_status = "verified" if published_date else "missing"
    if any(pattern in normalized for pattern in _MARKET_ACTIVITY_PATTERNS):
        page_type = "market_activity"
        materiality = "low"
        event_type = "market_activity"
    elif any(pattern in normalized for pattern in _MATERIAL_EVENT_PATTERNS):
        page_type = "company_event"
        materiality = "medium"
        event_type = "company_news"
    else:
        page_type = "dated_company_news" if published_date else "company_news_undated"
        materiality = "low" if published_date else "unverified"
        event_type = "company_news"

    return {
        "page_type": page_type,
        "citable_as_reference": True,
        "reference_reject_reason": "",
        "reference_materiality": materiality,
        "reference_event_type": event_type,
        "reference_date_status": date_status,
    }


def _reference_event_signature(ticker: str, title: str) -> str:
    normalized = _normalize_match_text(title)
    normalized = re.sub(r"\d+(?:\.\d+)?", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return f"{ticker}|{normalized}"


def _build_reference_evidence(
    source_records: list[dict[str, Any]],
    *,
    used_source_indices: set[int],
    instrument_metadata: dict[str, dict[str, Any]],
    reference_datetime: dt.datetime,
    freshness_days: int,
    max_per_ticker: int = 2,
    max_total: int = 6,
) -> list[dict[str, Any]]:
    """Build deterministic source cards without another model or network call.

    Dated article pages are preferred.  An undated but clearly article-like,
    entity-matched source is still publishable as background, explicitly marked
    as date-unknown and never counted as Accepted Evidence.
    """
    candidates: list[dict[str, Any]] = []
    for source in source_records:
        source_index = _parse_source_index(source.get("source_index"))
        if source_index is None or source_index in used_source_indices:
            continue
        if not source.get("citable_as_reference"):
            continue
        matched = [str(x).upper() for x in (source.get("matched_tickers") or []) if str(x).strip()]
        if len(matched) != 1:
            continue
        published = _parse_date(source.get("published_date"))
        if published is not None:
            age = (reference_datetime.date() - published).days
            if age < 0 or age > freshness_days:
                continue
        elif not source.get("article_fetch_ok"):
            # An undated URL that could not be read locally has no defensible
            # recency signal.  Do not publish it merely because the provider
            # returned an official-looking path; that previously surfaced old
            # releases and navigation pages as if they were current news.
            continue
        title = str(source.get("title") or "").strip()
        if not title:
            continue
        item = dict(source)
        item["_ticker"] = matched[0]
        item["_published"] = published
        candidates.append(item)

    # Prefer material company-event articles over market-activity recaps, then
    # prefer known/newer dates.  This prevents turnover/ranking articles from
    # crowding out an actual operating update in the small report allowance.
    reference_page_rank = {
        "company_event": 4,
        "dated_company_news": 3,
        "company_news_undated": 2,
        "market_activity": 1,
    }
    candidates.sort(
        key=lambda item: (
            reference_page_rank.get(str(item.get("page_type") or ""), 0),
            1 if item.get("_published") is not None else 0,
            str(item.get("published_date") or ""),
            -int(item.get("source_index") or 0),
        ),
        reverse=True,
    )
    references: list[dict[str, Any]] = []
    per_ticker_count: Counter[str] = Counter()
    seen_signatures: set[str] = set()
    for source in candidates:
        if len(references) >= max_total:
            break
        ticker = str(source.get("_ticker") or "").upper()
        title = str(source.get("title") or "").strip()
        signature = _reference_event_signature(ticker, title)
        if per_ticker_count[ticker] >= max_per_ticker or signature in seen_signatures:
            continue
        published = source.get("_published")
        published_iso = published.isoformat() if isinstance(published, dt.date) else ""
        source_index = int(source.get("source_index"))
        url = str(source.get("url") or "").strip()
        canonical = _canonical_url(url)
        domain = str(source.get("source_domain") or _domain(url))
        source_quality, source_type = _source_quality(domain, ticker, instrument_metadata)
        snippet = str(source.get("snippet") or "").strip()
        summary = snippet if len(snippet) >= 20 else title
        summary = summary[:600]
        date_known = bool(published_iso)
        uid = hashlib.sha256(
            f"reference|{ticker}|{source_index}|{canonical}|{published_iso or 'unknown-date'}".encode("utf-8")
        ).hexdigest()[:24]
        references.append({
            "evidence_uid": uid,
            "evidence_id": None,
            "reference_id": f"R{len(references) + 1:03d}",
            "ticker": ticker,
            "related_tickers": [ticker],
            "scope": "ticker",
            "event_type": str(source.get("reference_event_type") or "company_news"),
            "content_type": "news_report",
            "title": title,
            "source_name": domain or "DashScope Web Search",
            "source_domain": domain,
            "source_type": source_type,
            "source_quality": source_quality,
            "source_quality_score": 90 if source_quality == "tier_1" else 70,
            "published_date": published_iso,
            "raw_published_date": str(source.get("published_date") or ""),
            "source_published_date": published_iso,
            "publication_date_status": "verified" if date_known else "not_provided_by_dashscope",
            "url": url,
            "canonical_url": canonical,
            "facts": [summary],
            "summary_zh": summary,
            "what_happened_zh": summary,
            "impact_direction": "neutral",
            "impact_horizon": "near_term",
            "materiality": str(source.get("reference_materiality") or ("low" if date_known else "unverified")),
            "portfolio_relevance": f"该来源与 {ticker} 直接相关，可作为联网背景材料。",
            "relevance_reason": f"本地实体匹配确认该来源直接关联 {ticker}。",
            "confidence": 0.55 if date_known else 0.40,
            "recency_tier": (
                "fresh_event" if date_known and (reference_datetime.date() - published).days <= 7
                else "recent_background" if date_known
                else "unknown_date"
            ),
            "source_verified": True,
            "verification_method": "dashscope_local_source_reference",
            "verification_level_zh": (
                "来源 URL 与日期已验证" if date_known
                else "来源 URL 已验证·日期未提供"
            ),
            "source_binding_method": "deterministic_source_reference",
            "article_fetch_ok": bool(source.get("article_fetch_ok")),
            "article_text_quality_ok": bool(source.get("article_text_quality_ok")),
            "snippet_fallback_ok": bool(snippet),
            "materiality_accepted": False,
            "summary_integrity_ok": True,
            "entity_role": "primary",
            "is_quote_page": False,
            "is_reference_page": False,
            "chronology_conflict": False,
            "accept": False,
            "decision_eligible": False,
            "source_note_only": True,
            "supports_action": "watch",
            "does_not_support": (
                "该来源仅作为已验证背景材料，不单独支撑方向性交易或基本面结论。"
                if date_known else
                "DashScope 未提供发布日期；该链接仅作背景导航，不支撑时效性判断、方向性交易或基本面结论。"
            ),
            "lane": "dashscope_single_search_reference",
            "dashscope_source_id": source.get("source_id"),
            "dashscope_source_index": source_index,
            "event_key": uid,
            "priority_score": 0.0,
        })
        per_ticker_count[ticker] += 1
        seen_signatures.add(signature)
    return references


def _compact_portfolio_context(
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]],
    top_tickers: list[str],
) -> dict[str, Any]:
    holdings = {str(h.get("ticker") or "").upper(): h for h in snapshot.get("holdings", [])}
    risk_items = {str(x.get("ticker") or "").upper(): x for x in ranking.get("items", [])}
    rc_items = {str(x.get("ticker") or "").upper(): x for x in metrics.get("risk_contributions", [])}
    items = []
    for ticker in top_tickers:
        holding = holdings.get(ticker, {})
        risk = risk_items.get(ticker, {})
        rc = rc_items.get(ticker, {})
        meta = instrument_metadata.get(ticker, {}) or {}
        items.append({
            "ticker": ticker,
            "name": holding.get("name") or meta.get("name") or ticker,
            "instrument_type": meta.get("instrument_type") or holding.get("instrument_type"),
            "theme": meta.get("theme") or meta.get("underlying_index"),
            "weight": holding.get("weight"),
            "risk_contribution": rc.get("risk_contribution"),
            "risk_weight_gap": rc.get("risk_weight_gap"),
            "composite_risk_score": risk.get("risk_score"),
            "rsi": holding.get("rsi"),
            "price_vs_ema50_pct": holding.get("price_vs_ema50_pct"),
            "return_5d": holding.get("return_5d"),
            "profit_loss_pct": holding.get("profit_loss_pct"),
        })
    return {
        "report_date": snapshot.get("report_date"),
        "base_currency": snapshot.get("base_currency"),
        "benchmark": snapshot.get("benchmark"),
        "portfolio_risk_score": metrics.get("portfolio_risk_score"),
        "portfolio_risk_level": metrics.get("portfolio_risk_level"),
        "portfolio_beta": metrics.get("portfolio_beta"),
        "max_drawdown_63d": metrics.get("max_drawdown_63d"),
        "max_drawdown_252d": metrics.get("max_drawdown_252d"),
        "below_ema50_weight": (metrics.get("aggregates") or {}).get("below_ema50_weight"),
        "top_risk_holdings": items,
    }


def _build_messages(
    context: dict[str, Any],
    targets: list[dict[str, Any]],
    freshness_days: int,
    *,
    reference_datetime: dt.datetime,
    supports_citation: bool,
) -> list[dict[str, str]]:
    """Build an evidence-first prompt whose final user turn is search-shaped.

    The previous prompt asked the model to search, interpret the whole portfolio,
    produce actions and satisfy a large JSON schema in the same turn.  On the
    DeepSeek third-party search path that polluted retrieval and repeatedly
    returned quote/community pages.  This version asks only for source-grounded
    news evidence; portfolio interpretation remains deterministic Python.
    """
    schema_item: dict[str, Any] = {
        "ticker": "one requested ticker",
        "source_title_hint": "copy the exact retrieved result title",
        "source_url_hint": "copy the exact retrieved URL only as a matching hint",
        "summary": "concise factual Chinese summary grounded only in that source",
        "materiality": "high|medium|low",
        "impact": "positive|negative|mixed|neutral",
        "confidence": 0.0,
    }
    if supports_citation:
        schema_item["source_ref"] = "[ref_1]"
    schema = {
        "evidence": [schema_item],
        "no_news_tickers": ["requested tickers with no qualifying recent article"],
    }

    target_lines: list[str] = []
    query_lines: list[str] = []
    start_date = (reference_datetime.date() - dt.timedelta(days=freshness_days)).isoformat()
    end_date = reference_datetime.date().isoformat()
    event_terms = (
        "earnings OR results OR guidance OR filing OR regulatory OR investigation OR lawsuit OR "
        "offering OR financing OR acquisition OR partnership OR contract OR launch OR appointment"
    )
    for position, target in enumerate(targets, start=1):
        ticker = str(target.get("ticker") or "").upper()
        name = str(target.get("name") or ticker).strip()
        aliases = [x for x in target.get("aliases") or [] if x != _normalize_match_text(ticker)][:2]
        official = [str(x) for x in (target.get("official_domains") or []) if str(x).strip()]
        target_lines.append(
            f"- {ticker} = {name}"
            + (f"; aliases: {', '.join(aliases)}" if aliases else "")
            + (f"; official domains: {', '.join(official)}" if official else "")
        )
        official_hint = (
            " Prefer " + ", ".join(official) + " or sec.gov."
            if official else " Prefer sec.gov or a major financial-news outlet."
        )
        query_lines.append(
            f"QUERY {position}: \"{name}\" {ticker} ({event_terms}) "
            f"after:{start_date}. Published on or before {end_date}." + official_hint
        )

    citation_rule = (
        "When the search layer inserts [ref_n], copy it exactly into source_ref. "
        if supports_citation else
        "This model path does not reliably support citation markers; copy the exact result title and URL as hints. "
    )
    system = (
        "You are a source-grounded company-news researcher. Return one strict JSON object and no Markdown. "
        + citation_rule
        + "Never invent, rewrite, shorten, or guess a source identity. The URL is only a matching hint; local code will accept it "
          "only when it exactly matches the provider source list. Include at most one article per ticker and zero items is allowed. "
          "Exclude quotes, stock profiles, forums, communities, homepages, tag pages, generic commentary and old background articles. "
          "Do not produce portfolio actions or general market analysis. Write summaries in Chinese.\n\n"
          "Exact research entities:\n" + "\n".join(target_lines) + "\n\nRequired JSON schema:\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )

    if len(query_lines) == 1:
        instruction = (
            f"Today is {end_date}. Search this exact company only. Find up to two dated, company-specific "
            "articles or individual official releases; do not substitute another company.\n"
        )
    else:
        instruction = (
            f"Today is {end_date}. Execute each numbered query separately inside this single response; "
            "do not merge the companies into one broad query. Find one dated, company-specific article per query when available.\n"
        )
    user = (
        instruction
        + "\n".join(query_lines)
        + f"\nReject results published after {end_date}. Reject quote, forum, profile, press-index, filing-index "
          "and other landing pages. After searching, return only the strict JSON requested in the system message."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _source_records(
    raw_sources: list[dict[str, Any]],
    *,
    reference_datetime: dt.datetime,
    targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    by_index: dict[int, dict[str, Any]] = {}
    seen_urls: set[str] = set()
    next_fallback_index = 1
    for position, source in enumerate(raw_sources, start=1):
        url = str(source.get("url") or source.get("link") or "").strip()
        canonical = _canonical_url(url)
        if not canonical or canonical in seen_urls:
            continue
        source_index = _parse_source_index(source.get("index"))
        if source_index is None or source_index in by_index:
            source_index = max(position, next_fallback_index)
            while source_index in by_index:
                source_index += 1
        next_fallback_index = source_index + 1
        source_date = _source_date(source, reference_datetime)
        record = {
            "source_index": source_index,
            "source_id": f"DS{source_index:03d}",
            "title": str(source.get("title") or source.get("name") or "").strip(),
            "url": url,
            "canonical_url": canonical,
            "source_domain": _domain(url),
            "published_date": source_date,
            "date_provenance": "dashscope_source" if source_date else "",
            "snippet": str(source.get("snippet") or source.get("content") or source.get("summary") or "").strip(),
            "raw_source": source,
        }
        records.append(record)
        by_index[source_index] = record
        seen_urls.add(canonical)
    _annotate_source_relevance(records, targets)
    for record in records:
        record.update(_classify_source_for_reference(record))
    return records, by_index


def _labeled_publication_date(text: Any, reference_datetime: dt.datetime) -> dt.date | None:
    """Parse dates only when nearby text explicitly labels publication time.

    Arbitrarily scanning an index-page lead can mistake the date of the newest
    scheduled announcement for the page's own publication date.  Require a
    publication label before considering free page text.
    """
    value = re.sub(r"\s+", " ", str(text or ""))[:1200]
    if not value:
        return None
    patterns = (
        r"(?:发布时间|发布日期|发布于|更新于|发稿时间|时间)\s*[:：]?\s*([^|]{6,40})",
        r"(?:published|posted|updated|date)\s*(?:on|at)?\s*[:：]?\s*([^|]{6,40})",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        parsed = _parse_date_with_reference(match.group(1), reference_datetime)
        if parsed:
            return parsed
    return None


def _article_publication_date(
    article: dict[str, Any],
    reference_datetime: dt.datetime,
    *,
    source_record: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Extract a publication date from locally fetched page-owned metadata.

    The order intentionally prefers explicit HTML metadata.  Free article text
    is inspected only near the beginning of the document to avoid mistaking an
    unrelated historical date in the body for the publication date.
    """
    candidates = [
        ("article_meta", article.get("published_date")),
        ("article_url", article.get("final_url") or article.get("url")),
        ("article_title", article.get("title")),
        ("article_description", str(article.get("meta_description") or "")[:600]),
    ]
    for provenance, value in candidates:
        parsed = _parse_date_with_reference(value, reference_datetime)
        if parsed and parsed <= reference_datetime.date():
            return parsed.isoformat(), provenance
    page_type = str((source_record or {}).get("page_type") or "")
    if page_type not in {"official_landing", "announcement_index", "index_page", "reference_page"}:
        parsed = _labeled_publication_date(article.get("text"), reference_datetime)
        if parsed and parsed <= reference_datetime.date():
            return parsed.isoformat(), "article_labeled_text"
    return "", ""


def _enrich_sources_from_articles(
    records: list[dict[str, Any]],
    *,
    raw_model_evidence: Any,
    fetch_call: Callable[..., Any] | None,
    reference_datetime: dt.datetime,
    targets: list[dict[str, Any]],
    max_fetches: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Fetch a very small number of already-returned article URLs in parallel.

    This is metadata validation, not a second search: only URLs supplied by the
    one DashScope response are fetched.  There is no retry and the number of
    fetches is hard-capped for predictable latency and scale.
    """
    if fetch_call is None or max_fetches <= 0:
        return []

    requested_indices: set[int] = set()
    requested_urls: set[str] = set()
    if isinstance(raw_model_evidence, list):
        for raw in raw_model_evidence:
            if not isinstance(raw, dict):
                continue
            requested_indices.update(_extract_model_source_indices(raw))
            hint = _canonical_url(str(raw.get("source_url_hint") or raw.get("url") or ""))
            if hint:
                requested_urls.add(hint)

    candidates = [
        record for record in records
        if record.get("is_relevant") and record.get("citable_as_reference") and record.get("url")
    ]
    official_by_ticker = {
        str(target.get("ticker") or "").upper(): set(target.get("official_domains") or [])
        for target in targets
    }

    def is_primary_source(item: dict[str, Any]) -> bool:
        domain = str(item.get("source_domain") or "")
        if _domain_matches(domain, _REGULATOR_DOMAINS):
            return True
        matched = [str(x).upper() for x in (item.get("matched_tickers") or [])]
        return any(_domain_matches(domain, official_by_ticker.get(ticker, set())) for ticker in matched)

    page_rank = {
        "company_event": 4,
        "dated_company_news": 3,
        "company_news_undated": 2,
        "market_activity": 1,
    }
    candidates.sort(
        key=lambda item: (
            page_rank.get(str(item.get("page_type") or ""), 0),
            1 if not item.get("published_date") else 0,
            1 if int(item.get("source_index") or -1) in requested_indices else 0,
            1 if str(item.get("canonical_url") or "") in requested_urls else 0,
            1 if is_primary_source(item) else 0,
            1 if _is_trusted_news_domain(str(item.get("source_domain") or "")) else 0,
            -int(item.get("source_index") or 0),
        ),
        reverse=True,
    )
    candidates = candidates[:max_fetches]
    if not candidates:
        return []

    fetched: list[dict[str, Any]] = []

    def do_fetch(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        url = str(record.get("url") or "")
        try:
            article = fetch_call(url, timeout=timeout_seconds, max_chars=4000)
            if not isinstance(article, dict):
                article = {"url": url, "ok": False, "error": "article_fetch_not_object"}
        except Exception as exc:  # noqa: BLE001 - diagnostics must survive a hostile page.
            article = {"url": url, "ok": False, "error": str(exc)}
        return record, article

    with ThreadPoolExecutor(max_workers=min(3, len(candidates))) as executor:
        futures = [executor.submit(do_fetch, record) for record in candidates]
        for future in as_completed(futures):
            record, article = future.result()
            date_value, date_provenance = _article_publication_date(
                article,
                reference_datetime,
                source_record=record,
            )
            original_date = str(record.get("published_date") or "")
            if original_date:
                record["date_provenance"] = record.get("date_provenance") or "dashscope_source"
            elif date_value:
                record["published_date"] = date_value
                record["date_provenance"] = date_provenance
            record["article_fetch_attempted"] = True
            record["article_fetch_ok"] = bool(article.get("ok"))
            record["article_text_quality_ok"] = bool(article.get("article_text_quality_ok"))
            record["article_fetch_error"] = str(article.get("error") or article.get("quality_reason") or "")
            record["article_final_url"] = str(article.get("final_url") or "")
            record["article_meta_description"] = str(article.get("meta_description") or "")[:800]
            record["article_text"] = str(article.get("text") or "")[:4000]
            fetched.append({
                "source_index": record.get("source_index"),
                "url": record.get("url"),
                "ok": bool(article.get("ok")),
                "published_date": record.get("published_date") or "",
                "date_provenance": record.get("date_provenance") or "",
                "error": record.get("article_fetch_error") or "",
            })

    # A fetched title can be more precise than the search title.  Re-run entity
    # and page classification after enrichment, but preserve the provider title
    # as the visible identity unless it was missing.
    _annotate_source_relevance(records, targets)
    for record in records:
        record.update(_classify_source_for_reference(record))
    return sorted(fetched, key=lambda x: int(x.get("source_index") or 0))


def _title_similarity(left: str, right: str) -> float:
    a, b = _normalize_match_text(left), _normalize_match_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    return difflib.SequenceMatcher(None, a, b).ratio()


def _extract_model_source_indices(item: dict[str, Any]) -> list[int]:
    values: list[Any] = [
        item.get("source_index"), item.get("source_ref"), item.get("citation_ref"),
        item.get("reference"), item.get("ref"),
    ]
    citations = item.get("citations") or item.get("source_refs") or []
    if isinstance(citations, (str, int, float)):
        citations = [citations]
    values.extend(citations if isinstance(citations, list) else [])
    # Citation markers may be injected inside a summary/title string by the search
    # plug-in rather than placed in the requested JSON field.
    for key in ("source_title_hint", "source_title", "summary"):
        values.extend(re.findall(r"\[ref_(\d+)\]|\[(\d+)\]", str(item.get(key) or ""), flags=re.IGNORECASE))

    out: list[int] = []
    for value in values:
        if isinstance(value, tuple):
            value = next((part for part in value if part), None)
        parsed = _parse_source_index(value)
        if parsed is not None and parsed not in out:
            out.append(parsed)
    return out


def _resolve_source_for_item(
    item: dict[str, Any],
    *,
    ticker: str,
    source_by_index: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, str | None]:
    """Resolve model evidence against actual DashScope sources.

    Resolution order: explicit/citation ref, exact-ish title match, unique article
    source for the entity, then unique entity source.  This keeps identity local
    while avoiding false rejection when a third-party model cannot reproduce the
    title byte-for-byte.
    """
    deferred_error: str | None = None
    explicit_indices = _extract_model_source_indices(item)
    if explicit_indices:
        matched_sources = []
        for source_index in explicit_indices:
            source = source_by_index.get(source_index)
            if source is None:
                continue
            if ticker in (source.get("matched_tickers") or []):
                matched_sources.append(source)
        if len(matched_sources) == 1:
            return matched_sources[0], "citation_or_source_index", None
        if len(matched_sources) > 1:
            citable = [x for x in matched_sources if x.get("citable_as_reference")]
            if len(citable) == 1:
                return citable[0], "citation_or_source_index", None
            deferred_error = "multiple_cited_sources_for_ticker"
        elif any(index in source_by_index for index in explicit_indices):
            deferred_error = "source_not_relevant_to_ticker"
        else:
            deferred_error = "source_index_not_in_dashscope_sources"

    relevant = [
        source for source in source_by_index.values()
        if ticker in (source.get("matched_tickers") or []) and source.get("is_relevant")
    ]
    url_hint = str(item.get("source_url_hint") or item.get("url") or "").strip()
    canonical_hint = _canonical_url(url_hint)
    if canonical_hint:
        matched_urls = [
            source for source in relevant
            if str(source.get("canonical_url") or "") == canonical_hint
        ]
        if len(matched_urls) == 1:
            return matched_urls[0], "source_url_hint", None
        if not matched_urls:
            # A third-party search model can copy a redirect/canonical variant
            # that is absent from search_info.  Treat it as a failed hint, then
            # continue with local title/entity resolution instead of rejecting
            # an otherwise bindable source.
            deferred_error = "source_url_hint_not_in_dashscope_sources"

    title_hint = str(item.get("source_title_hint") or item.get("source_title") or "").strip()
    if title_hint:
        scored = sorted(
            ((_title_similarity(title_hint, str(source.get("title") or "")), source) for source in relevant),
            key=lambda pair: pair[0], reverse=True,
        )
        if scored and scored[0][0] >= 0.58:
            if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.04:
                return scored[0][1], "source_title_hint", None

    article_sources = [source for source in relevant if source.get("citable_as_reference")]
    if len(article_sources) == 1:
        return article_sources[0], "unique_entity_article_source", None
    if title_hint:
        return None, "", deferred_error or "source_title_hint_not_matched"
    if len(relevant) == 1:
        return relevant[0], "unique_entity_source", None
    if not relevant:
        return None, "", deferred_error or "no_relevant_dashscope_source_for_ticker"
    return None, "", deferred_error or "multiple_relevant_sources_require_title_hint"


def _validate_model_evidence(
    raw_items: Any,
    *,
    source_by_index: dict[int, dict[str, Any]],
    allowed_tickers: set[str],
    instrument_metadata: dict[str, dict[str, Any]],
    reference_datetime: dt.datetime,
    freshness_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    seen_tickers: set[str] = set()
    seen_source_indices: set[int] = set()

    if not isinstance(raw_items, list):
        return accepted, [{"reason": "evidence_not_list", "raw": raw_items}], Counter({"evidence_not_list": 1})

    for raw in raw_items:
        item = dict(raw) if isinstance(raw, dict) else {"raw": raw}
        item_reasons: list[str] = []
        ticker = str(item.get("ticker") or "").upper().strip()
        if ticker not in allowed_tickers:
            item_reasons.append("ticker_not_allowed")
        if ticker in seen_tickers:
            item_reasons.append("duplicate_ticker")

        source, binding_method, binding_error = _resolve_source_for_item(
            item, ticker=ticker, source_by_index=source_by_index,
        )
        source_index = _parse_source_index((source or {}).get("source_index"))
        if binding_error:
            item_reasons.append(binding_error)
        elif source_index in seen_source_indices:
            item_reasons.append("duplicate_source_index")

        source_url = str((source or {}).get("url") or "").strip()
        canonical = _canonical_url(source_url)
        published = _parse_date((source or {}).get("published_date"))
        title = str((source or {}).get("title") or "").strip()
        summary = re.sub(r"\[ref_\d+\]|\[\d+\]", "", str(item.get("summary") or "")).strip()

        # Do not cascade source-dependent errors after binding already failed;
        # one precise rejection reason is more useful than three synthetic ones.
        if source is not None:
            if not canonical:
                item_reasons.append("invalid_source_url")
            if not title:
                item_reasons.append("missing_source_title")
            if not source.get("citable_as_reference"):
                item_reasons.append("source_not_article_page")
            if published is None:
                item_reasons.append("invalid_or_missing_source_date")
            else:
                age = (reference_datetime.date() - published).days
                if age < 0:
                    item_reasons.append("future_date")
                elif age > freshness_days:
                    item_reasons.append("outside_freshness_window")
        if len(summary) < 20:
            item_reasons.append("summary_too_short")

        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5

        if item_reasons:
            for reason in item_reasons:
                reasons[reason] += 1
            rejected.append({
                "ticker": ticker or None,
                "source_index": source_index,
                "source_title_hint": item.get("source_title_hint") or item.get("source_title"),
                "title": title or None,
                "url": source_url or None,
                "published_date": published.isoformat() if published else None,
                "reasons": item_reasons,
                "raw": item,
            })
            continue

        assert source is not None and source_index is not None and published is not None
        domain = str(source.get("source_domain") or _domain(source_url))
        source_quality, source_type = _source_quality(domain, ticker, instrument_metadata)
        evidence_uid = hashlib.sha256(
            f"{ticker}|{source_index}|{canonical}|{published.isoformat()}".encode("utf-8")
        ).hexdigest()[:24]
        recency_tier = "fresh_event" if (reference_datetime.date() - published).days <= 7 else "recent_background"
        accepted.append({
            "evidence_uid": evidence_uid,
            "evidence_id": None,
            "ticker": ticker,
            "related_tickers": [ticker],
            "scope": "ticker",
            "event_type": "material_event",
            "title": title,
            "source_name": domain or "DashScope Web Search",
            "source_domain": domain,
            "source_type": source_type,
            "source_quality": source_quality,
            "source_quality_score": 90 if source_quality == "tier_1" else 75,
            "published_date": published.isoformat(),
            "raw_published_date": str(source.get("published_date") or ""),
            "source_published_date": published.isoformat(),
            "url": source_url,
            "canonical_url": canonical,
            "facts": [summary],
            "summary_zh": summary,
            "impact_direction": str(item.get("impact") or "neutral").lower(),
            "materiality": str(item.get("materiality") or "medium").lower(),
            "portfolio_relevance": summary,
            "confidence": confidence,
            "recency_tier": recency_tier,
            "source_verified": True,
            "verification_method": "dashscope_local_source_binding",
            "source_binding_method": binding_method,
            "article_fetch_ok": False,
            "snippet_fallback_ok": True,
            "materiality_accepted": True,
            "summary_integrity_ok": True,
            "entity_role": "primary",
            "is_quote_page": False,
            "is_reference_page": False,
            "chronology_conflict": False,
            "accept": True,
            "lane": "dashscope_single_search",
            "dashscope_source_id": source.get("source_id"),
            "dashscope_source_index": source_index,
            "event_key": evidence_uid,
        })
        seen_tickers.add(ticker)
        seen_source_indices.add(source_index)

    for index, item in enumerate(accepted, start=1):
        item["evidence_id"] = f"E{index:03d}"
    return accepted, rejected, reasons


def _build_deterministic_source_evidence(
    source_records: list[dict[str, Any]],
    *,
    existing: list[dict[str, Any]],
    instrument_metadata: dict[str, dict[str, Any]],
    reference_datetime: dt.datetime,
    freshness_days: int,
) -> list[dict[str, Any]]:
    """Promote independently verifiable fresh sources without model dependence.

    The model is useful for summarisation, but it must not be a single point of
    failure.  A fresh official/regulatory release, or a fresh material-event
    article from a trusted financial publisher, is valid source evidence even
    when the third-party model omits it or emits malformed JSON.
    """
    used_tickers = {str(item.get("ticker") or "").upper() for item in existing}
    used_indices = {
        int(item.get("dashscope_source_index"))
        for item in existing if item.get("dashscope_source_index") is not None
    }
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for source in source_records:
        source_index = _parse_source_index(source.get("source_index"))
        if source_index is None or source_index in used_indices:
            continue
        if not source.get("is_relevant") or not source.get("citable_as_reference"):
            continue
        matched = [str(x).upper() for x in (source.get("matched_tickers") or []) if str(x).strip()]
        if len(matched) != 1 or matched[0] in used_tickers:
            continue
        published = _parse_date(source.get("published_date"))
        if published is None:
            continue
        age = (reference_datetime.date() - published).days
        if age < 0 or age > freshness_days:
            continue
        ticker = matched[0]
        domain = str(source.get("source_domain") or "")
        quality, source_type = _source_quality(domain, ticker, instrument_metadata)
        official_or_regulator = quality == "tier_1"
        trusted_news = _is_trusted_news_domain(domain)
        material_page = str(source.get("page_type") or "") == "company_event"
        if not official_or_regulator and not (trusted_news and material_page):
            continue
        title = str(source.get("title") or "").strip()
        if len(title) < 8:
            continue
        summary = (
            str(source.get("article_meta_description") or "").strip()
            or str(source.get("snippet") or "").strip()
            or title
        )
        summary = re.sub(r"\s+", " ", summary)[:700]
        score = (100 if official_or_regulator else 80) + max(0, freshness_days - age)
        candidates.append((score, ticker, {
            "source": source,
            "published": published,
            "summary": summary,
            "source_quality": quality,
            "source_type": source_type,
        }))

    candidates.sort(key=lambda item: (item[0], item[2]["published"].isoformat()), reverse=True)
    out: list[dict[str, Any]] = []
    for _, ticker, bundle in candidates:
        if ticker in used_tickers:
            continue
        source = bundle["source"]
        published = bundle["published"]
        source_index = int(source.get("source_index"))
        canonical = str(source.get("canonical_url") or _canonical_url(str(source.get("url") or "")))
        uid = hashlib.sha256(
            f"deterministic|{ticker}|{source_index}|{canonical}|{published.isoformat()}".encode("utf-8")
        ).hexdigest()[:24]
        out.append({
            "evidence_uid": uid,
            "evidence_id": None,
            "ticker": ticker,
            "related_tickers": [ticker],
            "scope": "ticker",
            "event_type": str(source.get("reference_event_type") or "company_news"),
            "title": str(source.get("title") or ""),
            "source_name": str(source.get("source_domain") or "DashScope Web Search"),
            "source_domain": str(source.get("source_domain") or ""),
            "source_type": bundle["source_type"],
            "source_quality": bundle["source_quality"],
            "source_quality_score": 90 if bundle["source_quality"] == "tier_1" else 80,
            "published_date": published.isoformat(),
            "raw_published_date": str(source.get("published_date") or ""),
            "source_published_date": published.isoformat(),
            "url": str(source.get("url") or ""),
            "canonical_url": canonical,
            "facts": [bundle["summary"]],
            "summary_zh": bundle["summary"],
            "what_happened_zh": bundle["summary"],
            "impact_direction": "neutral",
            "materiality": "medium" if str(source.get("page_type") or "") == "company_event" else "low",
            "portfolio_relevance": f"该来源为 {ticker} 的近期、可验证公司事件来源。",
            "relevance_reason": f"本地实体匹配与来源元数据确认该来源直接关联 {ticker}。",
            "confidence": 0.68 if bundle["source_quality"] == "tier_1" else 0.58,
            "recency_tier": "fresh_event" if (reference_datetime.date() - published).days <= 7 else "recent_background",
            "source_verified": True,
            "verification_method": "deterministic_dashscope_source_event",
            "source_binding_method": "deterministic_source_metadata",
            "article_fetch_ok": bool(source.get("article_fetch_ok")),
            "article_text_quality_ok": bool(source.get("article_text_quality_ok")),
            "snippet_fallback_ok": bool(source.get("snippet")),
            "materiality_accepted": True,
            "summary_integrity_ok": True,
            "entity_role": "primary",
            "is_quote_page": False,
            "is_reference_page": False,
            "chronology_conflict": False,
            "accept": True,
            "decision_eligible": True,
            "supports_action": "watch",
            "does_not_support": "单一来源仅支持持续观察，不单独支撑方向性仓位调整。",
            "lane": "dashscope_single_search_deterministic",
            "dashscope_source_id": source.get("source_id"),
            "dashscope_source_index": source_index,
            "event_key": uid,
        })
        used_tickers.add(ticker)
        used_indices.add(source_index)
    return out


def _assign_evidence_ids(items: list[dict[str, Any]]) -> None:
    for index, item in enumerate(items, start=1):
        item["evidence_id"] = f"E{index:03d}"


def _build_advice(
    payload: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    accepted: list[dict[str, Any]],
) -> dict[str, Any]:
    if not accepted:
        return default_fallback_advice(
            snapshot, metrics, ranking,
            reason="单次 DashScope 联网研究未返回通过本地实体、来源与日期校验的证据。",
        )

    assessment = payload.get("portfolio_assessment") if isinstance(payload.get("portfolio_assessment"), dict) else {}
    stance = str(assessment.get("portfolio_stance") or "observe").lower()
    if stance not in _ALLOWED_STANCES:
        stance = "observe"
    try:
        confidence = max(0.0, min(1.0, float(assessment.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5

    weights = {str(h.get("ticker") or "").upper(): float(h.get("weight") or 0.0) for h in snapshot.get("holdings", [])}
    top_tickers = [str(t).upper() for t in ranking.get("top_risk_tickers") or []]
    raw_actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    raw_by_ticker = {
        str(item.get("ticker") or "").upper(): item
        for item in raw_actions if isinstance(item, dict)
    }
    evidence_by_ticker = {
        ticker: [item for item in accepted if str(item.get("ticker") or "").upper() == ticker]
        for ticker in top_tickers
    }
    actions = []
    for priority, ticker in enumerate(top_tickers[:6], start=1):
        if ticker not in weights:
            continue
        raw = raw_by_ticker.get(ticker, {})
        action = str(raw.get("action") or "watch").lower()
        if action not in _ALLOWED_ACTIONS:
            action = "watch"
        evidence_ids = [str(x.get("evidence_id")) for x in evidence_by_ticker.get(ticker, []) if x.get("evidence_id")]
        if action in {"add", "trim", "reduce", "exit"} and not evidence_ids:
            action = "watch"
        try:
            action_conf = max(0.0, min(1.0, float(raw.get("confidence", confidence))))
        except (TypeError, ValueError):
            action_conf = confidence
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            reason = "结合量化风险排序与本轮已验证联网来源进行观察。" if evidence_ids else "当前没有通过本地校验的标的事件证据。"
        current = weights[ticker]
        actions.append({
            "ticker": ticker,
            "action": action,
            "priority": priority,
            "action_timing": "conditional" if action in {"add", "trim", "reduce", "exit"} else "monitor",
            "current_weight": current,
            "target_weight_min": current,
            "target_weight_max": current,
            "confidence": action_conf,
            "portfolio_reason": reason,
            "technical_reason": "量化指标与风险贡献由 Python 计算。",
            "news_reason": reason if evidence_ids else "本轮没有通过本地实体、来源与日期校验的标的事件证据。",
            "execute_if": [],
            "cancel_or_upgrade_if": [],
            "further_reduce_if": [],
            "monitoring_items": [f"{ticker} 风险贡献变化", "已验证事件后续进展"],
            "thresholds": [],
            "evidence_ids": evidence_ids,
        })

    executive = assessment.get("executive_summary") if isinstance(assessment.get("executive_summary"), list) else []
    executive = [str(x).strip() for x in executive if str(x).strip()][:5]
    if not executive:
        executive = ["本报告结合确定性组合指标与一次 DashScope 联网研究生成。"]
    risk_level = str(metrics.get("portfolio_risk_level") or "medium").lower()
    return {
        "language": "zh-CN",
        "report_mode": "ai",
        "ai_analysis_available": True,
        "portfolio_stance": stance,
        "risk_level": risk_level,
        "confidence": confidence,
        "executive_summary": executive,
        "portfolio_analysis": {
            "trend_view": str(assessment.get("trend_view") or ""),
            "concentration_view": str(assessment.get("concentration_view") or ""),
            "risk_view": str(assessment.get("risk_view") or ""),
            "relative_performance_view": str(assessment.get("relative_performance_view") or ""),
            "news_view": str(assessment.get("news_view") or ""),
        },
        "key_risks": [],
        "actions": actions,
        "watch_items": [],
        "data_limitations": [
            "联网研究最多执行一次；未执行补搜或重试。",
            "证据通过本地实体、来源标题与安全 URL 提示绑定；URL 始终来自 DashScope 来源表，发布日期可由同一 URL 的有界本地页面校验补全。",
        ],
        "disclaimer": "本报告仅供研究参考，不构成投资建议。",
    }


def _usage_tokens(usage: dict[str, Any]) -> tuple[int, int, int]:
    def first_int(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
        return 0
    input_tokens = first_int("input_tokens", "prompt_tokens")
    output_tokens = first_int("output_tokens", "completion_tokens")
    total_tokens = first_int("total_tokens") or input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def run_portfolio_single_search(
    *,
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]],
    model: str = "deepseek-v4-flash",
    provider: str = "dashscope",
    generation_call: Callable[..., Any] | None = None,
    article_fetch_call: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run the one allowed DashScope web-search call and validate its output locally."""
    if provider.lower() != "dashscope":
        raise PortfolioSingleSearchUnavailable("Portfolio 单次联网模式仅支持 provider=dashscope。")
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key and generation_call is None:
        raise PortfolioSingleSearchUnavailable("DASHSCOPE_API_KEY 未配置。")

    top_limit = max(1, min(10, int(os.environ.get("PORTFOLIO_SINGLE_SEARCH_TOP_TICKERS", "5"))))
    capabilities = _model_search_capabilities(model)
    # Real DeepSeek runs showed that one turbo call does not reliably execute
    # multiple numbered company searches: the returned source list was often
    # monopolised by the most popular company and omitted the highest-risk name.
    # Keep third-party models to one exact entity per call. Qwen search models,
    # which support provider-side scope/freshness controls, may still use up to 3.
    entity_cap = 3 if capabilities["supports_freshness"] else 1
    default_entity_limit = entity_cap
    entity_limit = max(
        1,
        min(entity_cap, int(os.environ.get("PORTFOLIO_SINGLE_SEARCH_ENTITY_LIMIT", str(default_entity_limit)))),
    )
    requested_top_tickers = [str(t).upper() for t in (ranking.get("top_risk_tickers") or [])[:top_limit]]
    research_targets, omitted_search_tickers = _build_research_targets(
        snapshot, ranking, instrument_metadata,
        requested_limit=top_limit,
        entity_limit=entity_limit,
    )
    top_tickers = [str(target.get("ticker") or "").upper() for target in research_targets]
    if not top_tickers:
        raise PortfolioSingleSearchUnavailable("没有可用于单次联网研究的 Top-risk 标的。")
    freshness_days = max(1, min(90, int(os.environ.get("PORTFOLIO_SINGLE_SEARCH_FRESHNESS_DAYS", "30"))))
    strategy = "turbo"  # Product requirement: fixed, not environment-overridable.
    reference_datetime = _parse_reference_datetime(
        (snapshot.get("run_timeline") or {}).get("snapshot_completed_at") or dt.datetime.now().astimezone()
    )
    context = _compact_portfolio_context(snapshot, metrics, ranking, instrument_metadata, top_tickers)
    messages = _build_messages(
        context,
        research_targets,
        freshness_days,
        reference_datetime=reference_datetime,
        supports_citation=capabilities["supports_citation"],
    )

    real_generation_call = generation_call is None
    if generation_call is None:
        try:
            import dashscope
        except ImportError as exc:
            raise PortfolioSingleSearchUnavailable("dashscope SDK 未安装。") from exc
        dashscope.base_http_api_url = os.environ.get("DASHSCOPE_API_BASE", "https://dashscope.aliyuncs.com/api/v1")
        generation_call = dashscope.Generation.call
    if article_fetch_call is None and real_generation_call:
        try:
            from .article_fetcher import _fetch_article_text
        except Exception:  # pragma: no cover - fetch enrichment is optional at runtime.
            article_fetch_call = None
        else:
            article_fetch_call = _fetch_article_text

    started_at = dt.datetime.now().astimezone()
    search_options = {
        "forced_search": True,
        "search_strategy": strategy,
        "enable_source": True,
    }
    if capabilities["supports_citation"]:
        search_options.update({
            "enable_citation": True,
            "citation_format": "[ref_<number>]",
        })
    if capabilities["supports_freshness"]:
        search_options["freshness"] = freshness_days
    response = generation_call(
        api_key=api_key,
        model=model,
        messages=messages,
        enable_search=True,
        search_options=search_options,
        result_format="message",
    )
    completed_at = dt.datetime.now().astimezone()

    content, raw_sources, response_meta = _response_content_sources_usage(response)
    if response_meta.get("code") and str(response_meta.get("code")) not in {"200", "OK", "Success"}:
        raise PortfolioSingleSearchOutputError(
            f"DashScope 调用失败：{response_meta.get('code')} {response_meta.get('message')}"
        )
    payload = _extract_json_object(content)
    source_records, source_by_index = _source_records(
        raw_sources,
        reference_datetime=completed_at,
        targets=research_targets,
    )
    if payload is None:
        payload = {}
        parse_error = "invalid_json"
    else:
        parse_error = ""

    fetch_max = max(0, min(6, int(os.environ.get("PORTFOLIO_SOURCE_FETCH_MAX", "3"))))
    try:
        fetch_timeout = float(os.environ.get("PORTFOLIO_SOURCE_FETCH_TIMEOUT_SECONDS", "5"))
    except (TypeError, ValueError):
        fetch_timeout = 5.0
    fetch_timeout = max(1.0, min(12.0, fetch_timeout))
    article_fetch_records = _enrich_sources_from_articles(
        source_records,
        raw_model_evidence=payload.get("evidence") if payload else [],
        fetch_call=article_fetch_call,
        reference_datetime=completed_at,
        targets=research_targets,
        max_fetches=fetch_max,
        timeout_seconds=fetch_timeout,
    )
    # Source objects are mutated in place by enrichment; rebuild the index map
    # so validation sees locally verified dates and page metadata.
    source_by_index = {
        int(item["source_index"]): item
        for item in source_records if item.get("source_index") is not None
    }

    model_accepted, rejected, reject_reasons = _validate_model_evidence(
        payload.get("evidence") if payload else [],
        source_by_index=source_by_index,
        allowed_tickers=set(top_tickers),
        instrument_metadata=instrument_metadata,
        reference_datetime=completed_at,
        freshness_days=freshness_days,
    )
    deterministic_accepted = _build_deterministic_source_evidence(
        source_records,
        existing=model_accepted,
        instrument_metadata=instrument_metadata,
        reference_datetime=completed_at,
        freshness_days=freshness_days,
    )
    accepted = model_accepted + deterministic_accepted
    _assign_evidence_ids(accepted)
    used_source_indices = {
        int(item.get("dashscope_source_index"))
        for item in accepted if item.get("dashscope_source_index") is not None
    }
    reference_evidence = _build_reference_evidence(
        source_records,
        used_source_indices=used_source_indices,
        instrument_metadata=instrument_metadata,
        reference_datetime=completed_at,
        freshness_days=freshness_days,
        max_per_ticker=max(1, min(3, int(os.environ.get("PORTFOLIO_REFERENCE_PER_TICKER", "2")))),
        max_total=max(1, min(12, int(os.environ.get("PORTFOLIO_REFERENCE_MAX_TOTAL", "6")))),
    )
    advice = _build_advice(
        payload,
        snapshot=snapshot,
        metrics=metrics,
        ranking=ranking,
        accepted=accepted,
    )
    usage = response_meta.get("usage") if isinstance(response_meta.get("usage"), dict) else {}
    input_tokens, output_tokens, total_tokens = _usage_tokens(usage)
    relevant_sources = [x for x in source_records if x.get("is_relevant")]
    relevant_article_sources = [x for x in relevant_sources if x.get("citable_as_reference")]
    latest_source_date = max(
        (str(x.get("published_date") or "") for x in relevant_article_sources if x.get("published_date")),
        default=None,
    )
    latest_accepted_date = max((str(x.get("published_date") or "") for x in accepted if x.get("published_date")), default=None)
    status = (
        "success" if accepted
        else "source_notes_only" if reference_evidence
        else "invalid_model_output" if parse_error
        else "no_valid_evidence"
    )
    diagnostics = {
        "status": status,
        "research_mode": "dashscope_single_search",
        "provider_used": "dashscope_builtin_search",
        "model": model,
        "search_strategy": strategy,
        "freshness_days": freshness_days,
        "search_query": messages[-1].get("content", ""),
        "citation_enabled": bool(capabilities["supports_citation"]),
        "citation_format": "[ref_<number>]" if capabilities["supports_citation"] else None,
        "provider_freshness_filter_enabled": bool(capabilities["supports_freshness"]),
        "provider_site_scope_supported": bool(capabilities["supports_site_scope"]),
        "model_search_capabilities": capabilities,
        "search_entity_limit": entity_limit,
        "search_target_strategy": (
            "single_exact_entity_for_third_party_model"
            if not capabilities["supports_freshness"]
            else "multi_entity_provider_scoped_search"
        ),
        "search_call_count": 1,
        "external_search_call_count": 0,
        "model_call_count": 1,
        "retry_count": 0,
        "gap_search_count": 0,
        "max_search_calls": 1,
        "search_call_budget_respected": True,
        "news_search_executed_at": completed_at.isoformat(),
        "search_started_at": started_at.isoformat(),
        "search_elapsed_seconds": round((completed_at - started_at).total_seconds(), 3),
        "request_id": response_meta.get("request_id"),
        "raw_source_count": len(raw_sources),
        "unique_source_count": len(source_records),
        "relevant_source_count": len(relevant_sources),
        "relevant_article_source_count": len(relevant_article_sources),
        "landing_or_index_source_count": len([
            x for x in source_records
            if str(x.get("page_type") or "") in {
                "official_landing", "announcement_index", "index_page", "reference_page"
            }
        ]),
        "irrelevant_source_count": len([x for x in source_records if not x.get("is_relevant")]),
        "requested_top_risk_tickers": requested_top_tickers,
        "search_target_tickers": top_tickers,
        "omitted_search_tickers": omitted_search_tickers,
        "search_targets": research_targets,
        "model_evidence_count": len(payload.get("evidence") or []) if isinstance(payload.get("evidence"), list) else 0,
        "valid_evidence_count": len(accepted),
        "model_valid_evidence_count": len(model_accepted),
        "deterministic_evidence_count": len(deterministic_accepted),
        "reference_evidence_count": len(reference_evidence),
        "citable_source_count": len(reference_evidence),
        "undated_reference_count": len([x for x in reference_evidence if not x.get("published_date")]),
        "local_reference_fallback_used": bool(reference_evidence and not accepted),
        "invalid_evidence_count": len(rejected),
        "invalid_evidence_reasons": dict(reject_reasons),
        "invalid_evidence_items": rejected,
        "accepted_evidence_count": len(accepted),
        "rejected_evidence_count": len(rejected),
        "latest_selected_event_date": latest_source_date,
        "latest_accepted_event_date": latest_accepted_date,
        "response_json_parse_error": parse_error or None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "usage": usage,
        "article_metadata_fetch_count": len(article_fetch_records),
        "article_metadata_fetch_ok_count": len([x for x in article_fetch_records if x.get("ok")]),
        "article_metadata_date_enriched_count": len([
            x for x in article_fetch_records
            if str(x.get("date_provenance") or "").startswith("article_")
        ]),
        "article_metadata_fetch_records": article_fetch_records,
        "article_metadata_fetch_max": fetch_max,
        "article_metadata_fetch_timeout_seconds": fetch_timeout,
        "source_binding_validation": (
            "本地精确实体匹配 + 安全 URL 提示匹配 + 来源标题提示 + 唯一文章来源回退"
            + (" + DashScope citation/source_index" if capabilities["supports_citation"] else "")
        ),
        "source_url_validation": (
            "模型 URL 只作规范化匹配提示；最终 URL 必须来自 DashScope 来源表"
        ),
        "date_validation": (
            f"决策证据日期来自 DashScope 元数据或对同一来源 URL 的本地页面校验，"
            f"不得晚于检索时间且不超过 {freshness_days} 天；"
            "背景来源允许日期未提供，但会明确标记且不计入决策证据"
        ),
        "dashscope_sources": [
            {
                "source_index": item.get("source_index"),
                "source_id": item.get("source_id"),
                "title": item.get("title"),
                "source_domain": item.get("source_domain"),
                "published_date": item.get("published_date"),
                "url": item.get("url"),
                "matched_tickers": item.get("matched_tickers") or [],
                "relevance_status": item.get("relevance_status"),
                "relevance_reason": item.get("relevance_reason"),
                "page_type": item.get("page_type"),
                "citable_as_reference": bool(item.get("citable_as_reference")),
                "reference_reject_reason": item.get("reference_reject_reason"),
                "reference_date_status": item.get("reference_date_status"),
                "date_provenance": item.get("date_provenance"),
                "article_fetch_attempted": bool(item.get("article_fetch_attempted")),
                "article_fetch_ok": bool(item.get("article_fetch_ok")),
                "article_fetch_error": item.get("article_fetch_error"),
            }
            for item in source_records
        ],
    }
    return {
        "status": status,
        "advice": advice,
        "evidence": accepted,
        "accepted_evidence": accepted,
        "rejected_evidence": rejected,
        "reference_evidence": reference_evidence,
        "sources": source_records,
        "raw_model_output": content,
        "raw_model_payload": payload,
        "diagnostics": diagnostics,
        "raw_results": source_records,
        "filtered_results": accepted + reference_evidence,
    }
