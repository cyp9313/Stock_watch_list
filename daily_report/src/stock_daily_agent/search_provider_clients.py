"""Structured external-news adapters and provider-neutral candidate filters.

The module deliberately does not know about final-note generation.  It returns
untrusted search candidates only; callers must still run the local evidence and
SSRF-protected article-fetch pipeline before a record can be cited.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import os
import re
import time
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


KNOWN_SEARCH_PROVIDERS = {"serper", "anspire", "serpapi", "dashscope", "searxng"}
DEFAULT_SEARCH_PROVIDER_PRIORITY = ["serper", "anspire", "serpapi", "dashscope", "searxng"]

FOCUS_FRESHNESS_DAYS = {
    "earnings": 180, "analyst_ratings": 90, "major_events": 30,
    "industry": 45, "industry_macro": 45, "macro": 30, "macro_risks": 30,
    "risks": 60, "risks_sentiment": 30, "sentiment_flows": 30,
    "fund_flows": 30, "holdings_outlook": 90, "earnings_outlook": 90,
    "breadth_rotation": 30, "institutional_demand": 45,
    "regulation_macro": 60, "technical_sentiment": 14,
}

_TRACKING_QUERY_KEYS = {"gclid", "fbclid", "ref", "source"}
_DROP_EXTENSIONS = (".apk", ".exe", ".dmg", ".zip", ".rar", ".mp4", ".webm", ".jpg", ".jpeg", ".png", ".gif")
_GENERIC_COMPANY_WORDS = {"inc", "inc.", "corp", "corp.", "corporation", "ltd", "ltd.", "plc", "company", "co", "co."}


def _parse_provider_priority_with_warnings(value: str | None) -> tuple[list[str], list[str]]:
    """Return de-duplicated known providers and non-fatal unknown-provider warnings."""
    raw = value if value is not None else os.environ.get("SEARCH_PROVIDER_PRIORITY", "")
    providers: list[str] = []
    warnings: list[str] = []
    for name in str(raw or "").split(","):
        normalized = name.strip().lower()
        if not normalized or normalized in providers:
            continue
        if normalized not in KNOWN_SEARCH_PROVIDERS:
            warnings.append(f"unknown_provider_ignored:{normalized}")
            continue
        providers.append(normalized)
    return (providers or list(DEFAULT_SEARCH_PROVIDER_PRIORITY), warnings)


def parse_provider_priority(value: str | None) -> list[str]:
    """Return the configured, de-duplicated provider order.

    Warnings are deliberately exposed through the production diagnostics rather
    than changing this small public helper's list-shaped API.
    """
    return _parse_provider_priority_with_warnings(value)[0]


def provider_priority_warnings(value: str | None) -> list[str]:
    """Return non-fatal unknown-provider diagnostics for a priority string."""
    return _parse_provider_priority_with_warnings(value)[1]


def anspire_available() -> bool:
    return bool(os.environ.get("ANSPIRE_API_KEY", "").strip())


def serpapi_available() -> bool:
    return bool(os.environ.get("SERPAPI_API_KEY", "").strip())


def canonicalize_url(url: str) -> str:
    """Normalize URLs for cross-provider dedupe without destroying article IDs."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    kept = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), host, path, "", urlencode(kept, doseq=True), ""))


def normalize_title(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(value or "").casefold()).strip()


def focus_freshness_days(focus: str, instrument_type: str = "") -> int:
    del instrument_type  # Reserved for future instrument-specific overrides.
    configured = os.environ.get("SEARCH_MAX_AGE_DAYS", "").strip()
    try:
        global_cap = max(1, int(configured)) if configured else 180
    except ValueError:
        global_cap = 180
    return min(FOCUS_FRESHNESS_DAYS.get(str(focus or "").strip().lower(), 60), global_cap)


def _report_datetime(report_date: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(report_date).replace("Z", "+00:00"))
        # A report date without a timezone is a calendar date, not local wall
        # time.  Treat it as UTC so server timezone cannot move freshness
        # windows or Anspire FromTime by one day.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def normalize_news_date(value: Any, *, report_date: str) -> datetime | None:
    """Parse common provider dates.  A returned datetime is always UTC."""
    if value is None or str(value).strip().lower() in {"", "unknown", "none", "null", "1970-01-01"}:
        return None
    now = _report_datetime(report_date)
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            seconds = float(value)
            if seconds > 10_000_000_000:  # Milliseconds.
                seconds /= 1000
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
            return None if parsed.year <= 1971 else parsed
        except (ValueError, OverflowError, OSError):
            return None
    text = re.sub(r"\s+", " ", str(value).strip())
    lowered = text.lower()
    if lowered == "yesterday" or text == "昨天":
        return now - timedelta(days=1)
    relative = re.search(r"(\d+)\s*(hour|hours|day|days|小时|天)\s*(ago|前)", lowered if not re.search(r"[\u4e00-\u9fff]", text) else text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        return now - timedelta(hours=amount) if unit in {"hour", "hours", "小时"} else now - timedelta(days=amount)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            for fmt in ("%Y/%m/%d", "%b %d, %Y", "%d %b %Y"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    parsed = None  # type: ignore[assignment]
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return None if parsed.year <= 1971 else parsed


def filter_evidence_by_freshness(
    items: list[dict[str, Any]], *, report_date: str, instrument_type: str = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    now = _report_datetime(report_date)
    tolerance = max(0, int(os.environ.get("SEARCH_FUTURE_TOLERANCE_DAYS", "1") or 1))
    for raw in items:
        item = dict(raw)
        parsed = normalize_news_date(item.get("source_date"), report_date=report_date)
        if parsed is None:
            item["published_at"] = None
            item["date_status"] = "unknown"
            accepted.append(item)
            continue
        item["published_at"] = parsed.isoformat().replace("+00:00", "Z")
        if parsed > now + timedelta(days=tolerance):
            item["date_status"] = "future"
            item["rejection_reasons"] = ["future_date"]
            rejected.append(item)
            continue
        max_age = focus_freshness_days(str(item.get("focus") or ""), instrument_type)
        if parsed < now - timedelta(days=max_age):
            item["date_status"] = "stale"
            item["rejection_reasons"] = ["stale_date"]
            rejected.append(item)
            continue
        item["date_status"] = "known"
        accepted.append(item)
    return accepted, rejected


def score_target_relevance(item: dict[str, Any], *, ticker: str, data: dict[str, Any], instrument_type: str) -> dict[str, Any]:
    text = " ".join(str(item.get(key) or "") for key in ("title", "facts", "relevance")).casefold()
    ticker_text = str(ticker or "").upper()
    names = []
    for key in ("LONG_NAME", "SHORT_NAME"):
        value = re.sub(r"\s+", " ", str(data.get(key) or "").strip())
        if value and value.upper() != ticker_text:
            names.append(value)
    itype = str(instrument_type or "").upper()
    direct_terms = ("earnings", "revenue", "eps", "guidance", "upgrade", "downgrade", "target price", "buyback", "acquisition", "ceo", "regulator", "财报", "营收", "评级", "目标价", "回购", "并购", "监管")
    macro_terms = ("inflation", "federal reserve", "interest rate", "yield", "tariff", "macro", "cpi", "通胀", "利率", "美联储", "宏观")
    sector_terms = ("semiconductor", "chip", "cloud", "software", "bank", "energy", "technology", "行业", "板块", "芯片")
    reasons: list[str] = []
    score = 0
    if ticker_text and re.search(rf"(?<![A-Z0-9]){re.escape(ticker_text)}(?![A-Z0-9])", text.upper()):
        score += 55
        reasons.append("ticker_exact_match")
    for name in names:
        words = [word for word in re.findall(r"[\w\u4e00-\u9fff]+", name.casefold()) if word not in _GENERIC_COMPANY_WORDS and len(word) > 2]
        if words and (name.casefold() in text or all(word in text for word in words[:2])):
            score += 45
            reasons.append("company_name_match")
            break
    if any(term in text for term in direct_terms):
        score += 15
        reasons.append("direct_event_term")
    if itype in {"ETF", "INDEX", "CRYPTO"}:
        if score >= 35:
            category = "direct_company_news"
        elif any(term in text for term in sector_terms):
            category = "sector_related_news"
        elif any(term in text for term in macro_terms):
            category = "macro_market_news"
        else:
            category = "irrelevant"
    elif score >= 55:
        category = "direct_company_news"
    elif any(term in text for term in sector_terms):
        category = "sector_related_news"
        score += 15
        reasons.append("sector_term")
    elif any(term in text for term in macro_terms):
        category = "macro_market_news"
        score += 8
        reasons.append("macro_term")
    else:
        category = "irrelevant"
    return {"target_relevance_score": min(100, score), "target_relevance_category": category, "target_relevance_reasons": reasons}


def admit_search_result(item: dict[str, Any], *, target_context: dict[str, Any]) -> tuple[bool, list[str]]:
    url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()
    facts = str(item.get("facts") or "").strip()
    lowered = f"{url} {title} {facts}".casefold()
    reasons: list[str] = []
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        reasons.append("invalid_url")
    if not title and not facts:
        reasons.append("empty_title_and_facts")
    if parsed.path.casefold().endswith(_DROP_EXTENSIONS):
        reasons.append("non_article_asset")
    junk_terms = ("coupon", "sign in", "login", "porn", "casino", "download apk", "free apk")
    if any(term in lowered for term in junk_terms):
        # APP is a valid ticker; only reject a genuine download-page signal.
        if not (str(target_context.get("ticker") or "").upper() == "APP" and any(x in lowered for x in ("revenue", "installs", "active users", "downloads growth"))):
            reasons.append("low_value_or_download_page")
    category = str(item.get("target_relevance_category") or "irrelevant")
    focus = str(item.get("focus") or "")
    if category == "irrelevant":
        reasons.append("irrelevant_target")
    elif category == "macro_market_news" and focus not in {"macro", "macro_risks", "regulation_macro", "risks"}:
        reasons.append("macro_not_allowed_for_focus")
    elif category == "sector_related_news" and focus not in {"industry", "industry_macro", "major_events", "risks"}:
        reasons.append("sector_not_allowed_for_focus")
    return not reasons, reasons


def prepare_search_candidates(
    raw_items: list[dict[str, Any]], *, ticker: str, data: dict[str, Any], report_date: str, max_items: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    instrument_type = str(data.get("INSTRUMENT_TYPE") or "EQUITY")
    fresh, rejected = filter_evidence_by_freshness(raw_items, report_date=report_date, instrument_type=instrument_type)
    accepted: list[dict[str, Any]] = []
    for raw in fresh:
        item = dict(raw)
        item.update(score_target_relevance(item, ticker=ticker, data=data, instrument_type=instrument_type))
        item["canonical_url"] = canonicalize_url(str(item.get("url") or ""))
        item["normalized_title"] = normalize_title(str(item.get("title") or ""))
        ok, reasons = admit_search_result(item, target_context={"ticker": ticker, "instrument_type": instrument_type})
        if ok:
            accepted.append(item)
        else:
            item["rejection_reasons"] = reasons
            rejected.append(item)
    return dedupe_search_items(accepted, max_items=max_items), rejected


def dedupe_search_items(items: Iterable[dict[str, Any]], *, max_items: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_url: dict[str, dict[str, Any]] = {}
    seen_titles: set[tuple[str, str]] = set()
    for raw in items:
        item = dict(raw)
        canonical = str(item.get("canonical_url") or canonicalize_url(str(item.get("url") or "")))
        title = str(item.get("normalized_title") or normalize_title(str(item.get("title") or "")))
        domain = urlparse(canonical).netloc
        if not canonical and not title:
            continue
        if canonical and canonical in by_url:
            previous = by_url[canonical]
            providers = list(dict.fromkeys([*(previous.get("provider_sources") or [previous.get("provider")]), item.get("provider")]))
            previous["provider_sources"] = [p for p in providers if p]
            continue
        title_key = (domain, title)
        if title and title_key in seen_titles:
            continue
        item["canonical_url"] = canonical
        item["normalized_title"] = title
        item["provider_sources"] = list(dict.fromkeys([*(item.get("provider_sources") or []), item.get("provider")]))
        if canonical:
            by_url[canonical] = item
        if title:
            seen_titles.add(title_key)
        out.append(item)
        if len(out) >= max_items:
            break
    return out


def _safe_error(exc: Exception, secret: str = "") -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text.replace(secret, "[REDACTED]") if secret else text


def _http_get(url: str, *, params: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    import requests
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise ValueError("provider_non_json_response") from exc


def _anspire_region_mode(ticker: str, language: str) -> int:
    override = os.environ.get("ANSPIRE_REGION_MODE", "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    upper = ticker.upper()
    if upper.endswith((".SS", ".SZ", ".BJ")) and str(language).lower().startswith("zh"):
        return 0
    if upper.endswith(".HK") or str(language).lower().startswith("zh"):
        return 2
    return 1


def _anspire_request_query(query: str, ticker: str) -> str:
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    if len(query) <= 64:
        return query
    protected = ticker.upper()
    words = query.split()
    kept: list[str] = []
    for word in words:
        candidate = " ".join([*kept, word])
        if len(candidate) > 64:
            break
        kept.append(word)
    compact = " ".join(kept).strip()
    if protected and protected not in compact.upper() and len(protected) <= 64:
        compact = f"{protected} {compact}"[:64].rstrip()
    return compact or query[:64].rstrip()


def run_anspire_raw_search(
    *, ticker: str, languages: list[str], queries: dict[str, list[tuple[str, str]]], report_date: str,
    max_per_query: int, http_get: Callable[..., dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    key = os.environ.get("ANSPIRE_API_KEY", "").strip()
    if not key:
        return [], [], ["provider_missing_key:anspire"]
    base = os.environ.get("ANSPIRE_API_BASE", "https://plugin.anspire.cn").strip().rstrip("/")
    path = os.environ.get("ANSPIRE_API_PATH", "/api/ntsearch/search").strip()
    url = f"{base}{path if path.startswith('/') else '/' + path}"
    timeout = float(os.environ.get("ANSPIRE_TIMEOUT", "15"))
    sleep_seconds = float(os.environ.get("ANSPIRE_SLEEP_SECONDS", "0.2"))
    callback = http_get or _http_get
    start = _report_datetime(report_date)
    all_items: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    errors: list[str] = []
    for language in languages:
        for query, focus in queries.get(str(language), []):
            request_query = _anspire_request_query(query, ticker)
            from_time = start - timedelta(days=focus_freshness_days(focus))
            params = {
                "query": request_query, "top_k": max_per_query,
                "FromTime": from_time.strftime("%Y-%m-%d 00:00:00"),
                "ToTime": start.strftime("%Y-%m-%d 23:59:59"),
                "search_type": "web", "region_mode": _anspire_region_mode(ticker, str(language)),
            }
            try:
                payload = callback(url, params=params, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "*/*"}, timeout=timeout)
                results = payload.get("results") or (payload.get("data") or {}).get("results") or []
                parsed: list[dict[str, Any]] = []
                for position, raw in enumerate(results[:max_per_query], start=1):
                    if not isinstance(raw, dict):
                        continue
                    result_url = str(raw.get("url") or "").strip()
                    result_title = str(raw.get("title") or "").strip()
                    result_facts = str(raw.get("content") or "").strip()
                    parsed_url = urlparse(result_url)
                    # Reject malformed provider objects as early as possible.
                    # They cannot become auditable final evidence later either.
                    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                        continue
                    if not result_title and not result_facts:
                        continue
                    raw_date = str(raw.get("date") or "unknown").strip()
                    source_date = raw_date if normalize_news_date(raw_date, report_date=report_date) is not None else "unknown"
                    item = {
                        "title": result_title, "source": parsed_url.netloc.removeprefix("www."),
                        "source_date": source_date, "url": result_url,
                        "facts": result_facts, "relevance": f"Anspire query: {query}; focus={focus}",
                        "sentiment_hint": "BEAR" if focus in {"risks", "risks_sentiment", "macro_risks"} else "MIX",
                        "query": query, "language": language, "focus": focus, "engine": "anspire_web", "provider": "anspire",
                        "position": position, "provider_score": raw.get("score"), "raw_item": raw,
                    }
                    parsed.append(item)
                all_items.extend(parsed)
                calls.append({"query": query, "request_query": request_query, "language": language, "focus": focus, "count": len(parsed), "region_mode": params["region_mode"]})
            except Exception as exc:
                errors.append(f"anspire:{focus}:{_safe_error(exc, key)}")
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return all_items, calls, errors


def flatten_serpapi_news_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten direct Google News, stories and highlights while ignoring navigation."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for parent_position, raw in enumerate(payload.get("news_results") or [], start=1):
        if not isinstance(raw, dict):
            continue
        group_title = str(raw.get("title") or "")
        candidates = [raw]
        highlight = raw.get("highlight")
        if isinstance(highlight, dict):
            candidates.append(highlight)
        candidates.extend(item for item in (raw.get("stories") or []) if isinstance(item, dict))
        for candidate in candidates:
            link = str(candidate.get("link") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            copied = dict(candidate)
            copied.setdefault("position", candidate.get("position") or parent_position)
            if candidate is not raw:
                copied["parent_group_title"] = group_title
            out.append(copied)
    return out


def run_serpapi_raw_search(
    *, ticker: str, languages: list[str], queries: dict[str, list[tuple[str, str]]], report_date: str,
    max_per_query: int, http_get: Callable[..., dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not key:
        return [], [], ["provider_missing_key:serpapi"]
    url = os.environ.get("SERPAPI_API_BASE", "https://serpapi.com/search.json").strip()
    timeout = float(os.environ.get("SERPAPI_TIMEOUT", "15"))
    sleep_seconds = float(os.environ.get("SERPAPI_SLEEP_SECONDS", "0.2"))
    callback = http_get or _http_get
    all_items: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    errors: list[str] = []
    for language in languages:
        is_chinese = str(language).lower().startswith("zh")
        gl, hl = ("cn", "zh-cn") if is_chinese else ("us", "en")
        for query, focus in queries.get(str(language), []):
            request_query = f"{query} when:{focus_freshness_days(focus)}d"
            params = {"engine": os.environ.get("SERPAPI_ENGINE", "google_news"), "q": request_query, "gl": gl, "hl": hl, "api_key": key}
            try:
                payload = callback(url, params=params, headers={"Accept": "application/json"}, timeout=timeout)
                if payload.get("error"):
                    raise ValueError("provider_api_error")
                parsed: list[dict[str, Any]] = []
                for raw in flatten_serpapi_news_results(payload)[:max_per_query]:
                    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
                    parsed.append({
                        "title": str(raw.get("title") or "").strip(), "source": str((source or {}).get("name") or urlparse(str(raw.get("link") or "")).netloc).strip(),
                        "source_date": str(raw.get("iso_date") or raw.get("date") or "unknown").strip(), "url": str(raw.get("link") or "").strip(),
                        "facts": str(raw.get("snippet") or "").strip(), "relevance": f"SerpAPI Google News query: {query}; focus={focus}",
                        "sentiment_hint": "BEAR" if focus in {"risks", "risks_sentiment", "macro_risks"} else "MIX",
                        "query": query, "language": language, "focus": focus, "engine": "google_news", "provider": "serpapi",
                        "position": raw.get("position"), "raw_item": raw,
                    })
                all_items.extend(parsed)
                calls.append({"query": query, "request_query": request_query, "language": language, "focus": focus, "count": len(parsed)})
            except Exception as exc:
                errors.append(f"serpapi:{focus}:{_safe_error(exc, key)}")
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return all_items, calls, errors
