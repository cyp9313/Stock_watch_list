from __future__ import annotations

import json
import os
import re
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

try:
    from qwen_agent.tools.base import BaseTool
except Exception:  # Allows static checks before qwen-agent is installed.
    class BaseTool:  # type: ignore
        name = ""
        description = ""
        parameters: dict = {}

from .config import RunContext
from .notes import parse_notes_payload, validate_notes, render_notes_text, notes_to_jsonable
from .utils import parse_tool_params, json_dumps, run_python_script, ToolError, ensure_within_dir, strip_markdown_code_fence

_CONTEXT: RunContext | None = None


def set_context(ctx: RunContext) -> None:
    global _CONTEXT
    _CONTEXT = ctx


def get_context() -> RunContext:
    if _CONTEXT is None:
        raise ToolError("RunContext is not initialized. Call set_context(ctx) before creating tools.")
    return _CONTEXT


def _read_text(path: Path, max_chars: int = 12000) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) > max_chars:
        return text[: max_chars // 2] + "\n\n...[TRUNCATED]...\n\n" + text[-max_chars // 2 :]
    return text


def _technical_summary(data: dict[str, Any]) -> str:
    last = float(data.get("LAST_CLOSE", 0) or 0)
    pct = float(data.get("PCT", 0) or 0)
    currency = data.get("CURRENCY", "USD")
    instrument_type = str(data.get("INSTRUMENT_TYPE") or "EQUITY").upper()
    lines = [
        f"标的: {data.get('SHORT_NAME', data.get('TICKER'))} ({data.get('TICKER')})",
        f"标的类型: {instrument_type}",
        f"行业: {data.get('SECTOR', '—')} / {data.get('INDUSTRY', '—')}",
        f"最新收盘价: {last:.4f} {currency} ({pct:+.2f}%)",
        f"52周区间: {float(data.get('FIFTY2W_LO', 0) or 0):.4f} - {float(data.get('FIFTY2W_HI', 0) or 0):.4f}，当前位于 {float(data.get('percentile_52w', 0) or 0):.1f}% 分位",
    ]
    if instrument_type in {"EQUITY", "ETF"}:
        lines.append(
            f"市值/资产: {float(data.get('MARKET_CAP', 0) or 0):.1f}B；Forward PE: {float(data.get('FW_PE', 0) or 0):.1f}；"
            f"TTM PE: {float(data.get('TTM_PE', 0) or 0):.1f}；PEG: {float(data.get('PEG_RATIO', 0) or 0):.2f}；"
            f"PS: {float(data.get('PS_RATIO', 0) or 0):.2f}；PB: {float(data.get('PB_RATIO', 0) or 0):.2f}；Beta: {float(data.get('BETA', 0) or 0):.2f}"
        )
    if instrument_type == "EQUITY":
        lines.append(
            f"分析师目标价: mean={float(data.get('TARGET_MEAN', 0) or 0):.4f}，high={float(data.get('TARGET_HI', 0) or 0):.4f}，"
            f"low={float(data.get('TARGET_LO', 0) or 0):.4f}，覆盖人数={int(data.get('ANALYST_CNT', 0) or 0)}，共识={data.get('ANALYST_RATING', '') or 'N/A'}"
        )
    lines.extend([
        f"均线多头数: {int(data.get('bull_ma_count', 0) or 0)}/6；MA5={float(data.get('ma5', 0) or 0):.4f}，MA20={float(data.get('ma20', 0) or 0):.4f}，MA50={float(data.get('ma50', 0) or 0):.4f}，MA200={float(data.get('ma200', 0) or 0):.4f}",
        f"MACD: DIF={float(data.get('macd_line', 0) or 0):.4f}，DEA={float(data.get('signal_line', 0) or 0):.4f}，Hist={float(data.get('hist_val', 0) or 0):.4f}",
        f"RSI(14): {float(data.get('rsi', 0) or 0):.1f}；KDJ: K={float(data.get('k_val', 0) or 0):.1f}/D={float(data.get('d_val', 0) or 0):.1f}/J={float(data.get('j_val', 0) or 0):.1f}",
        f"布林带: 上轨={float(data.get('bb_up', 0) or 0):.4f}，中轨={float(data.get('bb_mid', 0) or 0):.4f}，下轨={float(data.get('bb_dn', 0) or 0):.4f}，位置={float(data.get('bb_pct', 0) or 0):.1f}%",
        f"成交量比(vs MA20): {float(data.get('vol_ratio', 0) or 0):.2f}x；ATR(14): {float(data.get('atr14', 0) or 0):.4f}",
        f"风险量化: 20日年化波动率={float(data.get('REALIZED_VOL_20D_PCT', 0) or 0):.1f}%；63日最大回撤={float(data.get('MAX_DRAWDOWN_63D_PCT', 0) or 0):.1f}%；ATR占比={float(data.get('ATR_PCT', 0) or 0):.2f}%",
    ])
    chip = data.get("chip_profile_primary") or {}
    if isinstance(chip, dict) and chip.get("ok"):
        peaks = chip.get("top_peaks") or []
        peak_txt = "; ".join([f"{p.get('price')}({p.get('role')}, {p.get('distance_pct')}%)" for p in peaks[:3] if isinstance(p, dict)])
        lines.append(
            "筹码峰/Volume Profile(126d): "
            f"POC={float(chip.get('poc_price', 0) or 0):.4f}，距现价={float(chip.get('poc_distance_pct', 0) or 0):+.2f}%，"
            f"价值区间={float(chip.get('value_area_low', 0) or 0):.4f}-{float(chip.get('value_area_high', 0) or 0):.4f}，"
            f"上方筹码占比={float(chip.get('overhead_supply_ratio', 0) or 0)*100:.1f}%，下方支撑筹码占比={float(chip.get('support_volume_ratio', 0) or 0)*100:.1f}%，"
            f"chip_score={float(chip.get('chip_score', 50) or 50):.1f}，signal={chip.get('chip_signal', 'N/A')}，top_peaks={peak_txt}"
        )
    if data.get("technical_score") is not None:
        subs = data.get("technical_subscores") or {}
        lines.append(
            f"技术面综合分: {float(data.get('technical_score', 50) or 50):.1f}/100；"
            f"trend={float(subs.get('trend_score', 50) or 50):.1f}，momentum={float(subs.get('momentum_score', 50) or 50):.1f}，"
            f"chip={float(subs.get('chip_profile_score', 50) or 50):.1f}，volume={float(subs.get('volume_score', 50) or 50):.1f}，"
            f"signal={data.get('technical_signal', 'N/A')}"
        )
    return "\n".join(lines)


def _plain_text_from_llm_response(response: Any) -> str:
    """Best-effort extraction for Qwen-Agent LLM streaming responses."""
    if response is None:
        return ""
    if isinstance(response, list):
        if not response:
            return ""
        return _plain_text_from_llm_response(response[-1])
    if isinstance(response, dict):
        content = response.get("content", "")
    else:
        content = getattr(response, "content", str(response))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def _extract_json_payload(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from an LLM response if possible."""
    raw = strip_markdown_code_fence(text)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _dashscope_content_and_sources(response: Any) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Extract model text and DashScope search_info.search_results from SDK response."""
    output = _obj_get(response, "output", {}) or {}
    search_info = _obj_get(output, "search_info", {}) or {}
    raw_sources = _obj_get(search_info, "search_results", []) or []

    content = ""
    choices = _obj_get(output, "choices", []) or []
    if choices:
        first = choices[0]
        msg = _obj_get(first, "message", {}) or {}
        content = str(_obj_get(msg, "content", "") or "")

    return content, [x for x in raw_sources if isinstance(x, dict)], {
        "search_info": search_info if isinstance(search_info, dict) else {},
        "request_id": _obj_get(response, "request_id", ""),
        "code": _obj_get(response, "code", ""),
        "message": _obj_get(response, "message", ""),
    }


def _dashscope_source_record(src: dict[str, Any], idx: int, model: str) -> dict[str, Any]:
    url = str(src.get("url") or src.get("link") or "").strip()
    title = str(src.get("title") or src.get("name") or "").strip()
    snippet = str(src.get("snippet") or src.get("content") or src.get("summary") or "").strip()
    source_date = str(src.get("date") or src.get("published_date") or src.get("publish_time") or "unknown").strip()
    source_domain = _source_domain(url)
    return {
        "evidence_id": f"DS{idx:03d}",
        "title": title or source_domain or f"DashScope source {idx}",
        "source": source_domain or "DashScope WebSearch",
        "source_date": source_date or "unknown",
        "url": url,
        "facts": snippet or f"DashScope returned this source for the market research query. URL: {url}",
        "relevance": "DashScope enable_source=true search result; use as a verifiable source object, not as model-only memory.",
        "sentiment_hint": "MIX",
        "evidence_method": "dashscope_search_source",
        "source_domain": source_domain,
        "dashscope_index": src.get("index", idx),
        "dashscope_model": model,
        "raw_source": src,
    }


def _match_dashscope_source_id(item: dict[str, Any], source_records: list[dict[str, Any]]) -> str:
    url = _normalize_url_for_match(str(item.get("url") or ""))
    if not url:
        return ""
    for src in source_records:
        if _normalize_url_for_match(str(src.get("url") or "")) == url:
            return str(src.get("evidence_id") or "")
    return ""


def _load_current_data(ctx: RunContext) -> dict[str, Any]:
    if not ctx.data_file.exists():
        return {}
    try:
        return json.loads(ctx.data_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def infer_market_type(ticker: str) -> str:
    t = ticker.upper().strip()
    if t.endswith(".SS") or t.endswith(".SZ"):
        return "china_a"
    if t.endswith(".HK"):
        return "hong_kong"
    if t.endswith("-USD"):
        return "crypto"
    if t.startswith("^"):
        return "index"
    return "us_stock_or_etf"


def infer_search_languages(ticker: str) -> list[str]:
    override = os.environ.get("SEARXNG_LANGUAGE", "auto").strip()
    if override and override.lower() not in {"auto", ""}:
        if "," in override:
            return [x.strip() for x in override.split(",") if x.strip()]
        return [override]

    market = infer_market_type(ticker)
    if market == "china_a":
        return ["zh-CN"]
    if market == "hong_kong":
        return ["en-US", "zh-CN"]
    # 美股、ETF、指数、加密货币默认英文；美股消息面通常英文来源更及时、更可核验。
    return ["en-US"]


def _searxng_base_url() -> str:
    return os.environ.get("SEARXNG_URL", "").strip().rstrip("/")


def _searxng_search_url(base: str) -> str:
    if base.endswith("/search"):
        return base
    return urljoin(base + "/", "search")


def _parse_searxng_result(item: dict[str, Any], query: str, language: str, focus: str = "") -> dict[str, Any]:
    return {
        "title": str(item.get("title") or "").strip(),
        "source": str(item.get("engine") or item.get("source") or "SearXNG").strip(),
        "source_date": str(item.get("publishedDate") or item.get("published_date") or item.get("date") or "unknown").strip(),
        "url": str(item.get("url") or "").strip(),
        "facts": str(item.get("content") or item.get("snippet") or "").strip(),
        "relevance": f"Search query: {query}; language={language}; focus={focus}".strip(),
        "sentiment_hint": "MIX",
        "query": query,
        "language": language,
        "focus": focus,
        "engine": item.get("engine"),
        "score": item.get("score"),
    }


def _http_get_json(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        import requests
    except Exception as exc:
        raise ToolError("缺少 requests 依赖，请运行: python -m pip install requests") from exc

    resp = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "qwen-stock-skill-agent/0.3"})
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception as exc:
        raise ToolError(f"SearXNG 返回的不是 JSON。请确认 settings.yml 中 search.formats 含 json。响应前200字: {resp.text[:200]}") from exc


def _http_post_json(url: str, payload: dict[str, Any], timeout: float, headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        import requests
    except Exception as exc:
        raise ToolError("缺少 requests 依赖，请运行: python -m pip install requests") from exc

    merged_headers = {"User-Agent": "qwen-stock-skill-agent/0.6", "Content-Type": "application/json"}
    if headers:
        merged_headers.update(headers)
    resp = requests.post(url, json=payload, timeout=timeout, headers=merged_headers)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception as exc:
        raise ToolError(f"POST JSON API 返回的不是 JSON。响应前200字: {resp.text[:200]}") from exc


def _serper_api_key() -> str:
    return os.environ.get("SERPER_API_KEY", "").strip()


def _serper_base_url() -> str:
    return os.environ.get("SERPER_API_BASE", "https://google.serper.dev").strip().rstrip("/")


def _serper_endpoint(search_type: str) -> str:
    search_type = (search_type or "search").strip().lower()
    if search_type not in {"search", "news"}:
        search_type = "search"
    return f"{_serper_base_url()}/{search_type}"


def _serper_gl_hl(language: str) -> tuple[str, str]:
    lang = (language or "en-US").lower()
    if lang.startswith("zh"):
        if "hk" in lang or "tw" in lang:
            return "hk", "zh-tw"
        return "cn", "zh-cn"
    return os.environ.get("SERPER_GL", "us"), os.environ.get("SERPER_HL", "en")


def _parse_serper_result(item: dict[str, Any], query: str, language: str, focus: str = "", search_type: str = "search") -> dict[str, Any]:
    url = str(item.get("link") or item.get("url") or "").strip()
    source = str(item.get("source") or item.get("sitename") or _source_domain(url) or "Serper").strip()
    source_date = str(item.get("date") or item.get("publishedDate") or item.get("published_date") or "unknown").strip()
    snippet = str(item.get("snippet") or item.get("content") or item.get("summary") or "").strip()
    return {
        "title": str(item.get("title") or "").strip(),
        "source": source,
        "source_date": source_date or "unknown",
        "url": url,
        "facts": snippet,
        "relevance": f"Serper {search_type} query: {query}; language={language}; focus={focus}".strip(),
        "sentiment_hint": "MIX",
        "query": query,
        "language": language,
        "focus": focus,
        "engine": f"serper_{search_type}",
        "position": item.get("position"),
        "score": item.get("score"),
        "provider": "serper",
        "raw_item": item,
    }


def _quality_metrics(items: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    domains: dict[str, int] = {}
    unknown = 0
    high_quality = 0
    article_ok = 0
    article_text_ok = 0
    grade_counts: dict[str, int] = {}
    provider_counts: dict[str, int] = {}
    engine_counts: dict[str, int] = {}
    for item in items:
        domain = str(item.get("source_domain") or _source_domain(str(item.get("url") or item.get("final_url") or "")) or "unknown")
        domains[domain] = domains.get(domain, 0) + 1
        if str(item.get("source_date") or item.get("published_date") or "unknown").lower() in {"", "unknown", "none", "null"}:
            unknown += 1
        if int(item.get("source_quality_score") or _source_quality_score(item)) >= 75:
            high_quality += 1
        if item.get("article_fetch_ok") or item.get("ok"):
            article_ok += 1
        if _article_text_quality_ok(item):
            article_text_ok += 1
        grade = str(item.get("evidence_grade") or _evidence_grade(item))
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        prov = str(item.get("provider") or "unknown")
        provider_counts[prov] = provider_counts.get(prov, 0) + 1
        eng = str(item.get("engine") or item.get("source") or "unknown")
        engine_counts[eng] = engine_counts.get(eng, 0) + 1
    return {
        "provider": provider,
        "count": len(items),
        "unknown_date_count": unknown,
        "high_quality_count": high_quality,
        "top_domains": sorted(domains.items(), key=lambda x: x[1], reverse=True)[:12],
        "article_fetch_ok_count": article_ok,
        "article_text_quality_ok_count": article_text_ok,
        "evidence_grade_counts": dict(sorted(grade_counts.items())),
        "provider_counts": sorted(provider_counts.items(), key=lambda x: x[1], reverse=True)[:10],
        "engine_counts": sorted(engine_counts.items(), key=lambda x: x[1], reverse=True)[:10],
    }


def _run_searxng_raw_search(ctx: RunContext, ticker: str, languages: list[str], max_per_query: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    base = _searxng_base_url()
    if not base:
        return [], [], ["SEARXNG_URL 未配置。"]
    data = _load_current_data(ctx)
    time_range = os.environ.get("SEARXNG_TIME_RANGE", "month").strip()
    categories = os.environ.get("SEARXNG_CATEGORIES", "general,news").strip()
    engines = os.environ.get("SEARXNG_ENGINES", "").strip()
    timeout = float(os.environ.get("SEARXNG_TIMEOUT", "15"))
    sleep_s = float(os.environ.get("SEARXNG_SLEEP_SECONDS", "0.2"))
    all_items: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    errors: list[str] = []
    search_url = _searxng_search_url(base)
    for language in languages:
        for query, focus in _build_market_queries(ticker, data, str(language)):
            params_dict: dict[str, Any] = {"q": query, "format": "json", "language": language}
            if time_range:
                params_dict["time_range"] = time_range
            if categories:
                params_dict["categories"] = categories
            if engines:
                params_dict["engines"] = engines
            try:
                payload = _http_get_json(search_url, params_dict, timeout=timeout)
                raw = payload.get("results") or []
                parsed = [_parse_searxng_result(x, query=query, language=str(language), focus=focus) for x in raw[:max_per_query] if isinstance(x, dict)]
                for item in parsed:
                    item["provider"] = "searxng"
                    if focus in {"risks", "risks_sentiment", "macro_risks"}:
                        item["sentiment_hint"] = "BEAR"
                    elif focus in {"earnings", "analyst_ratings"}:
                        item["sentiment_hint"] = "MIX"
                all_items.extend(parsed)
                calls.append({"query": query, "language": language, "focus": focus, "engines": engines or "default", "categories": categories, "count": len(parsed)})
            except Exception as exc:
                errors.append(f"{query} [{language}]: {exc}")
            if sleep_s > 0:
                time.sleep(sleep_s)
    return all_items, calls, errors


def _run_serper_raw_search(ctx: RunContext, ticker: str, languages: list[str], max_per_query: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    api_key = _serper_api_key()
    if not api_key:
        return [], [], ["SERPER_API_KEY 未配置。"]
    data = _load_current_data(ctx)
    timeout = float(os.environ.get("SERPER_TIMEOUT", "15"))
    sleep_s = float(os.environ.get("SERPER_SLEEP_SECONDS", "0.2"))
    serper_types = [x.strip().lower() for x in os.environ.get("SERPER_TYPES", os.environ.get("SERPER_TYPE", "search")).split(",") if x.strip()]
    if not serper_types:
        serper_types = ["search"]
    all_items: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    errors: list[str] = []
    for language in languages:
        gl, hl = _serper_gl_hl(str(language))
        for query, focus in _build_market_queries(ticker, data, str(language)):
            for search_type in serper_types:
                endpoint = _serper_endpoint(search_type)
                payload = {"q": query, "gl": gl, "hl": hl, "num": max_per_query}
                try:
                    data_json = _http_post_json(endpoint, payload=payload, timeout=timeout, headers={"X-API-KEY": api_key})
                    result_key = "news" if search_type == "news" else "organic"
                    raw = data_json.get(result_key) or data_json.get("organic") or data_json.get("news") or []
                    parsed = [_parse_serper_result(x, query=query, language=str(language), focus=focus, search_type=search_type) for x in raw[:max_per_query] if isinstance(x, dict)]
                    for item in parsed:
                        if focus in {"risks", "risks_sentiment", "macro_risks"}:
                            item["sentiment_hint"] = "BEAR"
                    all_items.extend(parsed)
                    calls.append({"query": query, "language": language, "focus": focus, "type": search_type, "count": len(parsed)})
                except Exception as exc:
                    errors.append(f"Serper {search_type}: {query} [{language}]: {exc}")
                if sleep_s > 0:
                    time.sleep(sleep_s)
    return all_items, calls, errors


def _prepare_evidence_from_raw(ctx: RunContext, ticker: str, raw_items: list[dict[str, Any]], max_total: int, context_top_n: int, provider: str, fetch_articles: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items = _dedupe_evidence(raw_items, max_items=max(max_total * 2, max_total + 8))
    items = _rerank_evidence(items)
    max_unknown = int(os.environ.get("EVIDENCE_MAX_UNKNOWN_DATE", "6"))
    items = _limit_unknown_dates(items, max_unknown=max_unknown)[:max_total]
    article_records: list[dict[str, Any]] = []
    article_fetch_enabled = fetch_articles and os.environ.get("ARTICLE_FETCH_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
    if article_fetch_enabled and items:
        max_urls = int(os.environ.get("ARTICLE_FETCH_MAX_URLS", "10"))
        article_max_chars = int(os.environ.get("ARTICLE_FETCH_MAX_CHARS", "3500"))
        article_timeout = float(os.environ.get("ARTICLE_FETCH_TIMEOUT", "12"))
        items, article_records = _enrich_evidence_with_articles(items, max_urls=max_urls, max_chars=article_max_chars, timeout=article_timeout)
        items = _rerank_evidence(items)
    prefix = "SP" if provider == "serper" else "SX" if provider == "searxng" else "E"
    items = _assign_evidence_ids(items[:min(context_top_n, max_total)], prefix=prefix)
    return items, article_records

def _dedupe_evidence(items: list[dict[str, Any]], max_items: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip().lower()
        key = url or title
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max_items:
            break
    return out


def _source_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


HIGH_VALUE_SOURCE_PATTERNS = [
    ("investor.oracle.com", 100),
    ("oracle.com", 88),
    ("sec.gov", 96),
    ("reuters.com", 92),
    ("bloomberg.com", 90),
    ("cnbc.com", 86),
    ("finance.yahoo.com", 82),
    ("marketwatch.com", 78),
    ("nasdaq.com", 76),
    ("morningstar.com", 75),
    ("barrons.com", 74),
    ("investing.com", 68),
    ("seekingalpha.com", 66),
    ("benzinga.com", 64),
    ("tipranks.com", 56),
    ("marketbeat.com", 52),
    ("chartmill.com", 42),
    ("insidermonkey.com", 38),
    ("msn.com", 36),
    ("aol.com", 35),
    ("247wallst.com", 45),
    ("forbes.com", 60),
]


SOCIAL_OR_LOW_EVIDENCE_DOMAINS = {
    "facebook.com", "instagram.com", "youtube.com", "youtu.be", "x.com", "twitter.com",
    "tiktok.com", "reddit.com", "stocktwits.com", "pinterest.com",
}

HARD_FACT_KEYWORDS = [
    "revenue", "营收", "收入", "eps", "每股收益", "guidance", "指引", "rpo",
    "capex", "资本支出", "free cash flow", "fcf", "自由现金流", "debt", "债务",
    "financing", "融资", "downgrade", "upgrade", "降级", "上调", "下调", "target price", "目标价",
    "pe", "p/e", "forward pe", "市盈率", "valuation", "估值", "margin", "利润率",
    "profit", "利润", "cash flow", "现金流", "shares sold", "减持", "insider", "sec", "10-k", "10-q",
    "fy", "q1", "q2", "q3", "q4", "同比", "yoy", "%", "$", "亿", "万",
]

LOW_VALUE_SOURCE_HINTS = [
    "coupon", "login", "sign in", "forum", "reddit", "pinterest", "youtube",
    "stocktwits", "wikipedia", "dictionary", "pdfcoffee", "facebook", "instagram", "tiktok",
]


def _is_blocked_or_consent_text(text: str, url: str = "") -> bool:
    raw = (text or "").lower() + " " + (url or "").lower()
    bad_markers = [
        "consent.yahoo.com", "cookie consent", "accept cookies", "enable javascript",
        "please verify you are a human", "captcha", "are you a robot",
        "sign in to continue", "subscribe to continue", "access denied",
        "403 forbidden", "request blocked", "privacy choices",
    ]
    return any(x in raw for x in bad_markers)


def _article_text_quality_ok(item: dict[str, Any]) -> bool:
    text = str(item.get("article_text") or item.get("text") or "")
    meta = str(item.get("meta_description") or "")
    title = str(item.get("title") or "")
    url = str(item.get("url") or item.get("final_url") or "")
    min_chars = int(os.environ.get("ARTICLE_MIN_TEXT_CHARS", "800"))
    combined = " ".join([title, meta, text]).strip()
    if len(text) < min_chars:
        return False
    if _is_blocked_or_consent_text(combined, url):
        return False
    # A finance article should normally include at least one number/date and clear market context.
    if not re.search(r"\d", combined):
        return False
    domain = _source_domain(url)
    official_markers = [
        "investor.", "sec.gov", "nasdaq.com", "federalreserve.gov",
        "bls.gov", "treasury.gov", "bea.gov", "cmegroup.com",
    ]
    if any(x in domain for x in official_markers):
        return True
    finance_context = re.compile(
        r"\b(stock|shares?|market|index|nasdaq|s&p|dow|etf|fund flows?|"
        r"revenue|earnings|eps|guidance|capex|debt|valuation|target price|"
        r"inflation|cpi|federal reserve|fed|treasury|yields?|rates?|"
        r"bitcoin|crypto|semiconductor|technology|megacap|mega-cap)\b",
        re.I,
    )
    return bool(finance_context.search(combined))


def _note_has_forbidden_usd_chinese_yi(note: Any) -> bool:
    text = " ".join([
        str(getattr(note, "title", "")),
        str(getattr(note, "fact", "")),
        str(getattr(note, "logic", "")),
        str(getattr(note, "investment_meaning", "")),
    ])
    # Avoid ambiguous/usually wrong forms like "$17.2亿" for US financials. Use "$17.2B / 172亿美元" instead.
    return bool(re.search(r"\$\s*\d+(?:\.\d+)?\s*亿", text))


def _extract_numeric_tokens(text: str) -> list[str]:
    # Keep simple numeric anchors only; avoid trying to normalize every format.
    raw = re.findall(r"\d+(?:\.\d+)?", text or "")
    out: list[str] = []
    for x in raw:
        if len(x) <= 1 and x not in {"0", "1"}:
            continue
        if x not in out:
            out.append(x)
    return out[:12]


def _numeric_support_ratio(note: Any, record: dict[str, Any]) -> tuple[float, list[str]]:
    fact = str(getattr(note, "fact", ""))
    nums = _extract_numeric_tokens(fact)
    if not nums:
        return 1.0, []
    support = " ".join([
        str(record.get("support_text_excerpt") or ""),
        str(record.get("facts") or ""),
        str(record.get("article_text") or ""),
        str(record.get("text") or ""),
        str(record.get("meta_description") or ""),
    ])
    support_digits = re.sub(r"[^0-9.]", " ", support)
    missing = [n for n in nums if n not in support and n not in support_digits]
    return (len(nums) - len(missing)) / max(1, len(nums)), missing


def _required_focus_coverage(ticker: str, data: dict[str, Any]) -> list[str]:
    """Return evidence focus requirements appropriate for the instrument type.

    An explicit SERPER_REQUIRED_FOCUS_COVERAGE value still overrides these defaults.
    Set it to ``auto`` to use this instrument-aware mapping.
    """
    override = os.environ.get("SERPER_REQUIRED_FOCUS_COVERAGE", "auto").strip()
    if override and override.lower() not in {"auto", "default"}:
        return [x.strip() for x in override.split(",") if x.strip()]

    instrument_type = str(data.get("INSTRUMENT_TYPE") or "").upper()
    if not instrument_type:
        inferred = infer_market_type(ticker)
        instrument_type = {
            "index": "INDEX", "crypto": "CRYPTO",
            "us_stock_or_etf": "EQUITY", "china_a": "EQUITY",
            "hong_kong": "EQUITY",
        }.get(inferred, "OTHER")
    return {
        "EQUITY": ["earnings", "analyst_ratings", "risks", "major_events"],
        "ETF": ["major_events", "fund_flows", "holdings_outlook", "macro_risks"],
        "INDEX": ["macro", "breadth_rotation", "earnings_outlook", "risks"],
        "CRYPTO": ["major_events", "macro_sentiment", "institutional_demand", "regulation_macro"],
        "OTHER": ["major_events", "macro", "risks"],
    }.get(instrument_type, ["major_events", "macro", "risks"])


def _evaluate_evidence_sufficiency(items: list[dict[str, Any]], required_focus: list[str] | None = None) -> dict[str, Any]:
    required_focus = required_focus or ["major_events", "macro", "risks"]
    min_total = int(os.environ.get("SERPER_MIN_FINAL_EVIDENCE", "10"))
    min_ab = int(os.environ.get("SERPER_MIN_GRADE_AB", "6"))
    grades = [str(i.get("evidence_grade") or _evidence_grade(i)) for i in items]
    ab = sum(1 for g in grades if g in {"A", "B", "TECH"})
    focus_set = {str(i.get("focus") or "") for i in items}
    missing_focus = [f for f in required_focus if f not in focus_set]
    ok = len(items) >= min_total and ab >= min_ab and not missing_focus
    return {
        "ok": ok,
        "count": len(items),
        "grade_ab_count": ab,
        "required_focus": required_focus,
        "focus_coverage": sorted(x for x in focus_set if x),
        "missing_focus": missing_focus,
        "thresholds": {"min_total": min_total, "min_grade_ab": min_ab},
        "reason": "sufficient" if ok else f"insufficient: count={len(items)} min={min_total}, A/B={ab} min_ab={min_ab}, missing_focus={missing_focus}",
    }


def _source_quality_score(item: dict[str, Any]) -> int:
    url = str(item.get("url") or item.get("final_url") or "")
    domain = _source_domain(url)
    title = str(item.get("title") or "").lower()
    facts = str(item.get("facts") or item.get("meta_description") or item.get("text") or "").lower()
    if domain in SOCIAL_OR_LOW_EVIDENCE_DOMAINS or any(domain.endswith("." + d) for d in SOCIAL_OR_LOW_EVIDENCE_DOMAINS):
        return 18
    score = 50
    for pattern, weight in HIGH_VALUE_SOURCE_PATTERNS:
        if pattern in domain:
            score = max(score, weight)
            break
    if any(h in domain or h in title for h in LOW_VALUE_SOURCE_HINTS):
        score -= 30
    if str(item.get("source_date") or "unknown").lower() != "unknown":
        score += 8
    if len(str(item.get("facts") or "")) > 120:
        score += 4
    if any(x in facts for x in ["revenue", "eps", "guidance", "target price", "upgrade", "downgrade", "sec", "fy"]):
        score += 6
    return max(0, min(100, score))


def _evidence_grade(item: dict[str, Any]) -> str:
    """Return A/B/C/D evidence grade for audit and gating.

    A: high-value source with fetched usable article text, or primary IR/SEC record.
    B: reliable source snippet/metadata without full article text.
    C: opinion/secondary finance site or less complete evidence.
    D: social/low-value source; sentiment-only.
    """
    url = str(item.get("url") or item.get("final_url") or "")
    domain = _source_domain(url)
    score = int(item.get("source_quality_score") or _source_quality_score(item))
    has_article_text = _article_text_quality_ok(item)
    if domain in SOCIAL_OR_LOW_EVIDENCE_DOMAINS or any(domain.endswith("." + d) for d in SOCIAL_OR_LOW_EVIDENCE_DOMAINS):
        return "D"
    if (domain.startswith("investor.") or ".investor." in domain or any(x in domain for x in [
        "sec.gov", "federalreserve.gov", "bls.gov", "treasury.gov", "bea.gov"
    ])):
        return "A"
    if has_article_text and score >= 75:
        return "A"
    if score >= 75:
        return "B"
    if score >= 55:
        return "C"
    return "D"


def _evidence_allowed_uses(grade: str) -> str:
    return {
        "A": "hard_facts,analysis,sentiment",
        "B": "hard_facts_if_snippet_specific,analysis,sentiment",
        "C": "opinion_or_secondary_analysis,sentiment; avoid primary financial figures",
        "D": "sentiment_only; do not support financial figures, guidance, debt, capex, FCF, ratings",
        "TECH": "technical_metrics,technical_analysis",
    }.get(grade or "", "unknown")


def _evidence_origin(item: dict[str, Any]) -> str:
    method = str(item.get("evidence_method") or item.get("method") or "").lower()
    provider = str(item.get("provider") or "").lower()
    eid = str(item.get("evidence_id") or "").upper()
    if eid.startswith("A") or "article" in method:
        return "article_fetch"
    if eid.startswith("DS") or "dashscope" in method:
        return "dashscope_source"
    if provider == "serper" or eid.startswith("SP"):
        return "serper"
    if provider == "searxng" or eid.startswith("SX"):
        return "searxng"
    return provider or method or "unknown"


def _support_excerpt(item: dict[str, Any], max_chars: int = 280) -> str:
    txt = str(item.get("facts") or item.get("meta_description") or item.get("article_text") or item.get("text") or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_chars]


def _note_contains_hard_fact(note: Any) -> bool:
    text = " ".join([
        str(getattr(note, "title", "")),
        str(getattr(note, "fact", "")),
        str(getattr(note, "logic", "")),
        str(getattr(note, "investment_meaning", "")),
    ]).lower()
    return any(k.lower() in text for k in HARD_FACT_KEYWORDS)


def _annotate_evidence_record(item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item)
    item.setdefault("source_domain", _source_domain(str(item.get("url") or item.get("final_url") or "")))
    item.setdefault("source_quality_score", _source_quality_score(item))
    grade = _evidence_grade(item)
    item["evidence_grade"] = grade
    item["evidence_allowed_uses"] = _evidence_allowed_uses(grade)
    item["evidence_origin"] = _evidence_origin(item)
    item["support_text_excerpt"] = _support_excerpt(item)
    return item


def _rerank_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        item["source_domain"] = _source_domain(str(item.get("url") or item.get("final_url") or ""))
        item["source_quality_score"] = _source_quality_score(item)
        annotated = _annotate_evidence_record(item)
        item.update({k: annotated[k] for k in ["evidence_grade", "evidence_allowed_uses", "evidence_origin", "support_text_excerpt"] if k in annotated})
    return sorted(
        items,
        key=lambda x: (
            int(x.get("source_quality_score") or 0),
            0 if str(x.get("source_date") or "unknown").lower() == "unknown" else 1,
            len(str(x.get("facts") or "")),
        ),
        reverse=True,
    )


def _limit_unknown_dates(items: list[dict[str, Any]], max_unknown: int) -> list[dict[str, Any]]:
    if max_unknown < 0:
        return items
    out: list[dict[str, Any]] = []
    unknown_count = 0
    for item in items:
        is_unknown = str(item.get("source_date") or "unknown").lower() in {"", "unknown", "none", "null"}
        if is_unknown:
            if unknown_count >= max_unknown:
                continue
            unknown_count += 1
        out.append(item)
    return out


def _assign_evidence_ids(items: list[dict[str, Any]], prefix: str = "E") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items, start=1):
        copied = dict(item)
        copied.setdefault("evidence_id", f"{prefix}{i:03d}")
        copied = _annotate_evidence_record(copied)
        out.append(copied)
    return out


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _normalize_url_for_match(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _collect_verifiable_records(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """Collect evidence/article/source records that final notes are allowed to cite.

    V5.1 allows DashScope search *source objects* (DS-xxx) when enable_source=true
    returned concrete title/url records. It still does not allow model-only
    DashScope candidates that have no verifiable source object.
    """
    records: dict[str, dict[str, Any]] = {}
    evidence_payload = _load_json_file(ctx.evidence_file)
    for item in evidence_payload.get("items", []) if isinstance(evidence_payload.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("evidence_id") or "").strip()
        if not eid:
            continue
        item = dict(item)
        item.setdefault("evidence_method", evidence_payload.get("method", "evidence"))
        records[eid] = item

    if os.environ.get("ALLOW_DASHSCOPE_SOURCES_IN_NOTES", "true").strip().lower() not in {"0", "false", "no"}:
        dashscope_payload = _load_json_file(ctx.dashscope_sources_file)
        for item in dashscope_payload.get("items", []) if isinstance(dashscope_payload.get("items"), list) else []:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("evidence_id") or "").strip()
            url = str(item.get("url") or "").strip()
            if not eid or not url:
                continue
            item = dict(item)
            item.setdefault("evidence_method", "dashscope_search_source")
            records[eid] = item

    articles_payload = _load_json_file(ctx.articles_file)
    allow_low_quality_articles = os.environ.get("ALLOW_LOW_QUALITY_ARTICLE_RECORDS", "false").strip().lower() in {"1", "true", "yes"}
    for idx, item in enumerate(articles_payload.get("items", []) if isinstance(articles_payload.get("items"), list) else [], start=1):
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        if not allow_low_quality_articles and not item.get("article_text_quality_ok"):
            continue
        eid = str(item.get("evidence_id") or f"A{idx:03d}")
        article_record = dict(item)
        article_record.setdefault("evidence_id", eid)
        article_record.setdefault("evidence_method", "fetch_article_text")
        article_record.setdefault("source_date", item.get("published_date") or "unknown")
        article_record.setdefault("source", item.get("source_domain") or _source_domain(str(item.get("url") or item.get("final_url") or "")))
        article_record.setdefault("facts", item.get("meta_description") or item.get("text") or "")
        records[eid] = article_record

    return records


def _resolve_note_to_evidence(note: Any, records: dict[str, dict[str, Any]]) -> tuple[bool, dict[str, Any] | None, str]:
    # Technical notes are generated from deterministic local data and do not need URL evidence.
    if getattr(note, "is_technical", False):
        return True, {
            "evidence_id": note.evidence_id or "TECH",
            "evidence_method": "fetch_technical_data",
            "source": note.source or "fetch_and_calc.py / yfinance 技术指标",
            "source_date": note.source_date,
            "url": "",
            "evidence_grade": "TECH",
            "evidence_origin": "fetch_technical_data",
            "evidence_allowed_uses": "technical_metrics,technical_analysis",
            "support_text_excerpt": "Deterministic technical metrics generated by fetch_and_calc.py from yfinance data.",
        }, ""

    eid = str(getattr(note, "evidence_id", "") or "").strip()
    if eid and eid in records:
        return True, records[eid], ""

    # If the LLM forgot evidence_id, try exact URL matching; then report the id back for audit.
    note_url = _normalize_url_for_match(getattr(note, "url", ""))
    if note_url:
        for record in records.values():
            urls = [record.get("url"), record.get("final_url")]
            if any(_normalize_url_for_match(str(u or "")) == note_url for u in urls):
                return True, record, ""

    if eid:
        return False, None, f"evidence_id={eid!r} 不存在于本地 evidence.json/articles.json。"
    return False, None, "缺少 evidence_id，且无法通过 URL 匹配到本地 evidence/articles。"


class _ArticleTextParser(HTMLParser):
    """Small stdlib HTML text extractor; intentionally dependency-light."""

    def __init__(self) -> None:
        super().__init__()
        self.skip_depth = 0
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.meta_description = ""
        self.meta_date = ""
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if tag_l in {"script", "style", "noscript", "svg", "nav", "footer", "form"}:
            self.skip_depth += 1
        if tag_l == "title":
            self.in_title = True
        if tag_l == "meta":
            name = (attrs_d.get("name") or attrs_d.get("property") or "").lower()
            content = attrs_d.get("content") or ""
            if name in {"description", "og:description", "twitter:description"} and not self.meta_description:
                self.meta_description = content.strip()
            if name in {"article:published_time", "published_time", "date", "dc.date", "dc.date.issued", "pubdate"} and not self.meta_date:
                self.meta_date = content.strip()[:10]

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in {"script", "style", "noscript", "svg", "nav", "footer", "form"} and self.skip_depth > 0:
            self.skip_depth -= 1
        if tag_l == "title":
            self.in_title = False
        if tag_l in {"p", "br", "li", "h1", "h2", "h3", "div"} and self.skip_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not data or self.skip_depth > 0:
            return
        txt = unescape(data).strip()
        if not txt:
            return
        if self.in_title:
            self.title_parts.append(txt)
        # Keep paragraph-like text and meaningful short metadata text.
        if len(txt) >= 30 or any(ch.isdigit() for ch in txt):
            self.parts.append(txt)

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()

    @property
    def text(self) -> str:
        raw = " ".join(self.parts)
        raw = re.sub(r"\s+", " ", raw)
        return raw.strip()


def _fetch_article_text(url: str, timeout: float = 12, max_chars: int = 5000) -> dict[str, Any]:
    try:
        import requests
    except Exception as exc:
        raise ToolError("缺少 requests 依赖，请运行: python -m pip install requests") from exc

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; qwen-stock-skill-agent/0.5; +https://example.local)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "html" not in content_type.lower() and not resp.text.lstrip().startswith("<"):
        return {
            "url": url,
            "ok": False,
            "error": f"Unsupported content type: {content_type}",
            "status_code": resp.status_code,
        }
    parser = _ArticleTextParser()
    parser.feed(resp.text[: max(max_chars * 20, 120000)])
    text = parser.text
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + " ...[TRUNCATED]"
    record = {
        "url": url,
        "final_url": resp.url,
        "ok": bool(text or parser.meta_description),
        "status_code": resp.status_code,
        "title": parser.title,
        "meta_description": parser.meta_description,
        "published_date": parser.meta_date,
        "text": text,
        "text_chars": len(text),
        "source_domain": _source_domain(resp.url),
    }
    record["article_text_quality_ok"] = _article_text_quality_ok(record)
    if not record["article_text_quality_ok"]:
        if _is_blocked_or_consent_text(" ".join([parser.title, parser.meta_description, text]), resp.url):
            record["quality_reason"] = "blocked_or_consent_or_login_page"
        elif len(text) < int(os.environ.get("ARTICLE_MIN_TEXT_CHARS", "800")):
            record["quality_reason"] = f"text_too_short:{len(text)}"
        else:
            record["quality_reason"] = "missing_finance_context_or_numbers"
    return record


def _enrich_evidence_with_articles(items: list[dict[str, Any]], max_urls: int, max_chars: int, timeout: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if max_urls <= 0:
        return items, []
    enriched: list[dict[str, Any]] = []
    article_records: list[dict[str, Any]] = []
    candidates = sorted(items, key=lambda x: int(x.get("source_quality_score") or _source_quality_score(x)), reverse=True)
    urls_done = 0
    url_to_article: dict[str, dict[str, Any]] = {}
    for item in candidates:
        url = str(item.get("url") or "").strip()
        if not url or urls_done >= max_urls:
            break
        domain = _source_domain(url)
        # Skip sources that are commonly hostile to scraping or too low value unless they scored high.
        if int(item.get("source_quality_score") or _source_quality_score(item)) < 55 and not any(x in domain for x in ["oracle.com", "sec.gov"]):
            continue
        try:
            article = _fetch_article_text(url, timeout=timeout, max_chars=max_chars)
        except Exception as exc:
            article = {"url": url, "ok": False, "error": str(exc), "source_domain": domain}
        url_to_article[url] = article
        article_records.append(article)
        urls_done += 1

    for item in items:
        url = str(item.get("url") or "").strip()
        article = url_to_article.get(url)
        if article and article.get("ok"):
            item = dict(item)
            item["article_fetch_ok"] = True
            item["article_text_quality_ok"] = bool(article.get("article_text_quality_ok"))
            item["article_quality_reason"] = article.get("quality_reason", "")
            if article.get("published_date") and str(item.get("source_date") or "unknown").lower() == "unknown":
                item["source_date"] = article.get("published_date")
            title = article.get("title") or item.get("title")
            meta = article.get("meta_description") or ""
            body = article.get("text") or ""
            combined = " ".join(x for x in [meta, body] if x).strip()
            if combined and article.get("article_text_quality_ok"):
                item["article_text"] = combined[:max_chars]
                # facts remains compact but now grounded in fetched page text, not only SERP snippet.
                item["facts"] = (combined[:900].rsplit(" ", 1)[0] + " ...") if len(combined) > 900 else combined
            elif meta:
                item.setdefault("meta_description", meta)
            if title:
                item["title"] = title
        enriched.append(item)
    return enriched, article_records


def _build_market_queries(ticker: str, data: dict[str, Any], language: str) -> list[tuple[str, str]]:
    ticker_u = ticker.upper()
    name = str(data.get("LONG_NAME") or data.get("SHORT_NAME") or ticker_u).strip()
    instrument_type = str(data.get('INSTRUMENT_TYPE') or '').upper()
    market = instrument_type.lower() if instrument_type in {'EQUITY', 'ETF', 'INDEX', 'CRYPTO'} else infer_market_type(ticker_u)

    if language.lower().startswith("zh"):
        if market == "china_a":
            base = name if name and name != ticker_u else ticker_u
            return [
                (f"{base} {ticker_u} 最新 财报 营收 净利润 指引", "earnings"),
                (f"{base} {ticker_u} 研报 评级 目标价 上调 下调", "analyst_ratings"),
                (f"{base} {ticker_u} 最新 新闻 重大事件 监管", "major_events"),
                (f"{base} 行业 动态 竞争对手 政策", "industry_macro"),
                (f"{base} 股价 风险 估值 资金流向", "risks_sentiment"),
            ]
        if market == "hong_kong":
            return [
                (f"{name} {ticker_u} 最新 财报 收入 利润 指引", "earnings"),
                (f"{name} {ticker_u} 评级 目标价 大行 上调 下调", "analyst_ratings"),
                (f"{name} {ticker_u} 回购 监管 重大事件 最新", "major_events"),
                (f"{name} 行业 动态 竞争对手 宏观", "industry_macro"),
                (f"{name} 港股 风险 估值 资金流向", "risks_sentiment"),
            ]
        return [
            (f"{name} {ticker_u} 最新 新闻 财报 评级 目标价", "general"),
            (f"{name} {ticker_u} 风险 估值 行业 动态", "risks_sentiment"),
        ]

    # English defaults for US stocks, ETFs, indexes and crypto.
    if market in {"crypto", "CRYPTO".lower()}:
        asset = ticker_u.replace("-USD", "")
        return [
            (f"{asset} latest news price drivers ETF flows regulation", "major_events"),
            (f"{asset} market analysis on-chain flows macro rates risk sentiment", "macro_sentiment"),
            (f"{asset} institutional adoption ETF demand market outlook latest", "institutional_demand"),
            (f"{asset} crypto regulation SEC Federal Reserve latest news", "regulation_macro"),
            (f"{asset} technical selloff rally support resistance latest", "technical_sentiment"),
        ]
    if market in {"index", "INDEX".lower()}:
        index_name = name if name and name != ticker_u else ticker_u
        return [
            (f"{index_name} {ticker_u} latest market news major events", "major_events"),
            (f"{index_name} Federal Reserve inflation Treasury yields interest rates impact", "macro"),
            (f"{index_name} market breadth advance decline sector rotation mega cap concentration", "breadth_rotation"),
            (f"{index_name} constituent earnings outlook technology semiconductor AI demand", "earnings_outlook"),
            (f"QQQ Nasdaq 100 fund flows institutional positioning latest", "sentiment_flows"),
            (f"{index_name} downside risks valuation concentration volatility latest", "risks"),
        ]

    if market == "etf":
        return [
            (f"{name} {ticker_u} ETF latest news holdings flows", "major_events"),
            (f"{name} {ticker_u} fund flows inflows outflows institutional demand", "fund_flows"),
            (f"{name} {ticker_u} portfolio valuation PE PB dividend yield", "valuation"),
            (f"{name} {ticker_u} sector exposure top holdings earnings outlook", "holdings_outlook"),
            (f"{name} {ticker_u} macro rates inflation Fed risks", "macro_risks"),
        ]

    return [
        (f"{name} {ticker_u} latest earnings revenue EPS guidance", "earnings"),
        (f"{name} {ticker_u} analyst rating target price upgrade downgrade", "analyst_ratings"),
        (f"{name} {ticker_u} stock latest news last 30 days", "major_events"),
        (f"{name} {ticker_u} industry competitors latest demand growth", "industry"),
        (f"{name} {ticker_u} macro rates inflation Fed impact stock", "macro"),
        (f"{name} {ticker_u} valuation risks debt capex free cash flow", "risks"),
        (f"{name} {ticker_u} institutional flows market sentiment technical", "sentiment_flows"),
    ]


class ReadSkillTool(BaseTool):
    name = "read_stock_daily_skill"
    description = "读取原始 stock-daily-report 的 SKILL.md，用于确认报告流程、notes 质量规则、ticker 格式和输出结构。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_context()
        return json_dumps({
            "skill_file": str(ctx.paths.skill_file),
            "content": _read_text(ctx.paths.skill_file, max_chars=16000),
        })


class ReadTickerReferenceTool(BaseTool):
    name = "read_ticker_reference"
    description = "读取本项目内 ticker_formats.md，帮助判断 yfinance ticker 格式，例如美股、港股、加密货币、指数、ETF。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_context()
        return json_dumps({
            "reference_file": str(ctx.paths.ticker_reference),
            "content": _read_text(ctx.paths.ticker_reference, max_chars=10000),
        })


class ValidateTickerTool(BaseTool):
    name = "validate_ticker_format"
    description = "检查 ticker 是否大致符合 yfinance 格式。它不会联网确认代码存在，只做格式层面的预检。"
    parameters = {
        "type": "object",
        "properties": {"ticker": {"type": "string", "description": "用户输入的股票、ETF、指数或加密货币代码"}},
        "required": ["ticker"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        p = parse_tool_params(params)
        ticker = str(p.get("ticker", "")).strip().upper()
        patterns = {
            "us_stock_or_etf": r"^[A-Z][A-Z0-9.-]{0,9}$",
            "crypto_usd": r"^[A-Z0-9]{2,10}-USD$",
            "hong_kong": r"^\d{4,5}\.HK$",
            "china_shanghai": r"^\d{6}\.SS$",
            "china_shenzhen": r"^\d{6}\.SZ$",
            "index": r"^\^[A-Z0-9.]{2,10}$",
        }
        matched = [name for name, pattern in patterns.items() if re.match(pattern, ticker)]
        return json_dumps({
            "ticker": ticker,
            "valid_format": bool(matched),
            "matched_formats": matched,
            "market_type": infer_market_type(ticker),
            "recommended_search_languages": infer_search_languages(ticker),
            "warning": "格式预检通过不代表 yfinance 一定有数据。" if matched else "格式不符合常见 yfinance ticker 规则，请先读取 ticker reference 或让用户确认。",
        })


class FetchTechnicalDataTool(BaseTool):
    name = "fetch_technical_data"
    description = "执行 scripts/fetch_and_calc.py 获取 yfinance 数据并计算技术指标。必须在生成 notes 和 build_report 之前调用。"
    parameters = {
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
        "required": ["ticker"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        ticker = str(p.get("ticker") or ctx.ticker).strip().upper()
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        output = ensure_within_dir(ctx.data_file, ctx.run_dir)
        result = run_python_script(
            ctx.paths.scripts_dir / "fetch_and_calc.py",
            [ticker, str(output)],
            cwd=ctx.run_dir,
            timeout=240,
        )
        data = json.loads(output.read_text(encoding="utf-8"))
        return json_dumps({
            "ok": True,
            "data_file": str(output),
            "stdout_tail": result["stdout"],
            "technical_summary": _technical_summary(data),
            "key_data": {k: data.get(k) for k in [
                "TICKER", "SHORT_NAME", "LONG_NAME", "SECTOR", "INDUSTRY", "CURRENCY",
                "LAST_CLOSE", "PCT", "MARKET_CAP", "FW_PE", "TTM_PE", "TARGET_MEAN", "TARGET_HI", "TARGET_LO",
                "ANALYST_CNT", "ANALYST_RATING", "PEG_RATIO", "PS_RATIO", "PB_RATIO", "FUNDAMENTAL_SOURCES",
                "STOCKANALYSIS_DATA", "BETA", "DIV_YIELD", "bull_ma_count", "rsi", "macd_line", "signal_line", "hist_val",
                "k_val", "d_val", "j_val", "bb_pct", "vol_ratio", "atr14",
                "technical_score", "technical_signal", "technical_subscores",
                "chip_profile_primary", "chip_score", "data_end",
            ]},
        })


class SearXNGSearchTool(BaseTool):
    name = "searxng_search"
    description = (
        "使用用户自建 SearXNG 实例执行单次搜索，返回原始搜索结果。"
        "如果 SEARXNG_URL 未配置会返回 ok=false。美股建议 language=en-US；A股 zh-CN；港股可双语。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "language": {"type": "string", "description": "SearXNG language，例如 en-US、zh-CN、all；默认按 ticker 自动推断"},
            "time_range": {"type": "string", "description": "day/month/year 或空；默认 SEARXNG_TIME_RANGE/month"},
            "categories": {"type": "string", "description": "general,news 等；默认 SEARXNG_CATEGORIES/general,news"},
            "engines": {"type": "string", "description": "可选，逗号分隔，例如 bing,duckduckgo"},
            "max_results": {"type": "integer", "description": "最多返回结果数，默认 8"},
        },
        "required": ["query"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        p = parse_tool_params(params)
        query = str(p.get("query") or "").strip()
        if not query:
            return json_dumps({"ok": False, "errors": ["query 不能为空。"]})
        base = _searxng_base_url()
        if not base:
            return json_dumps({"ok": False, "errors": ["SEARXNG_URL 未配置，无法使用 searxng_search。"]})
        language = str(p.get("language") or os.environ.get("SEARXNG_LANGUAGE") or "en-US").strip()
        if language.lower() == "auto":
            language = infer_search_languages(get_context().ticker)[0]
        time_range = str(p.get("time_range") or os.environ.get("SEARXNG_TIME_RANGE") or "month").strip()
        categories = str(p.get("categories") or os.environ.get("SEARXNG_CATEGORIES") or "general,news").strip()
        engines = str(p.get("engines") or os.environ.get("SEARXNG_ENGINES") or "").strip()
        max_results = int(p.get("max_results") or 8)
        timeout = float(os.environ.get("SEARXNG_TIMEOUT", "15"))

        request_params: dict[str, Any] = {"q": query, "format": "json", "language": language}
        if time_range:
            request_params["time_range"] = time_range
        if categories:
            request_params["categories"] = categories
        if engines:
            request_params["engines"] = engines

        try:
            payload = _http_get_json(_searxng_search_url(base), request_params, timeout=timeout)
            raw_results = payload.get("results") or []
            results = [_parse_searxng_result(x, query=query, language=language) for x in raw_results[:max_results] if isinstance(x, dict)]
            return json_dumps({
                "ok": True,
                "method": "searxng_search",
                "query": query,
                "language": language,
                "count": len(results),
                "results": results,
            })
        except Exception as exc:
            return json_dumps({"ok": False, "errors": [f"SearXNG 搜索失败: {exc}"]})


class SearXNGMarketResearchTool(BaseTool):
    name = "searxng_market_research"
    description = (
        "优先使用用户自建 SearXNG 搜索引擎为 ticker 自动执行多轮市场消息面搜索。"
        "它按市场自动选择语言：美股/ETF/指数/加密货币默认 en-US，港股 en-US+zh-CN，A股 zh-CN。"
        "返回并自动保存 evidence.json，后续应基于这些证据调用 save_news_notes。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "technical_summary": {"type": "string"},
            "languages": {"type": "array", "items": {"type": "string"}, "description": "可选；不填则自动推断"},
            "max_results_per_query": {"type": "integer", "description": "每个 query 最多取几个结果，默认 8"},
            "max_total_results": {"type": "integer", "description": "原始候选池最多保留多少条去重证据，默认 40；传给模型的 evidence_file 由 EVIDENCE_CONTEXT_TOP_N 控制"},
        },
        "required": ["ticker"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        ticker = str(p.get("ticker") or ctx.ticker).strip().upper()
        base = _searxng_base_url()
        if not base:
            return json_dumps({
                "ok": False,
                "errors": ["SEARXNG_URL 未配置，无法使用 searxng_market_research。请配置 SEARXNG_URL 或改用 dashscope_market_research。"],
            })

        data = _load_current_data(ctx)
        languages = p.get("languages") or infer_search_languages(ticker)
        if isinstance(languages, str):
            languages = [x.strip() for x in languages.split(",") if x.strip()]
        max_per_query = int(p.get("max_results_per_query") or os.environ.get("SEARXNG_MAX_RESULTS_PER_QUERY") or 8)
        max_total = int(p.get("max_total_results") or os.environ.get("SEARXNG_MAX_TOTAL_RESULTS") or 40)
        context_top_n = int(os.environ.get("EVIDENCE_CONTEXT_TOP_N", "18"))
        time_range = os.environ.get("SEARXNG_TIME_RANGE", "month").strip()
        categories = os.environ.get("SEARXNG_CATEGORIES", "general,news").strip()
        engines = os.environ.get("SEARXNG_ENGINES", "").strip()
        timeout = float(os.environ.get("SEARXNG_TIMEOUT", "15"))
        sleep_s = float(os.environ.get("SEARXNG_SLEEP_SECONDS", "0.2"))

        all_items: list[dict[str, Any]] = []
        errors: list[str] = []
        calls: list[dict[str, Any]] = []
        search_url = _searxng_search_url(base)
        for language in languages:
            for query, focus in _build_market_queries(ticker, data, str(language)):
                params_dict: dict[str, Any] = {"q": query, "format": "json", "language": language}
                if time_range:
                    params_dict["time_range"] = time_range
                if categories:
                    params_dict["categories"] = categories
                if engines:
                    params_dict["engines"] = engines
                try:
                    payload = _http_get_json(search_url, params_dict, timeout=timeout)
                    raw = payload.get("results") or []
                    parsed = [_parse_searxng_result(x, query=query, language=str(language), focus=focus) for x in raw[:max_per_query] if isinstance(x, dict)]
                    # Give a weak sentiment hint by focus. The agent remains responsible for final BULL/BEAR/MIX classification.
                    for item in parsed:
                        if focus in {"risks", "risks_sentiment", "macro_risks"}:
                            item["sentiment_hint"] = "BEAR"
                        elif focus in {"earnings", "analyst_ratings"}:
                            item["sentiment_hint"] = "MIX"
                    all_items.extend(parsed)
                    calls.append({"query": query, "language": language, "focus": focus, "engines": engines or "default", "categories": categories, "count": len(parsed)})
                except Exception as exc:
                    errors.append(f"{query} [{language}]: {exc}")
                if sleep_s > 0:
                    time.sleep(sleep_s)

        ctx.raw_results_file.write_text(json_dumps({
            "ticker": ticker,
            "method": "searxng_market_research_raw",
            "raw_count": len(all_items),
            "items": all_items,
            "calls": calls,
            "errors": errors,
        }), encoding="utf-8")

        items = _dedupe_evidence(all_items, max_items=max(max_total * 2, max_total + 8))
        items = _rerank_evidence(items)
        max_unknown = int(os.environ.get("EVIDENCE_MAX_UNKNOWN_DATE", "6"))
        items = _limit_unknown_dates(items, max_unknown=max_unknown)[:max_total]
        ctx.reranked_evidence_file.write_text(json_dumps({
            "ticker": ticker,
            "method": "searxng_market_research_reranked",
            "count": len(items),
            "items": items,
        }), encoding="utf-8")

        article_records: list[dict[str, Any]] = []
        article_fetch_enabled = os.environ.get("ARTICLE_FETCH_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
        if article_fetch_enabled and items:
            max_urls = int(os.environ.get("ARTICLE_FETCH_MAX_URLS", "10"))
            article_max_chars = int(os.environ.get("ARTICLE_FETCH_MAX_CHARS", "3500"))
            article_timeout = float(os.environ.get("ARTICLE_FETCH_TIMEOUT", "12"))
            items, article_records = _enrich_evidence_with_articles(
                items,
                max_urls=max_urls,
                max_chars=article_max_chars,
                timeout=article_timeout,
            )
            items = _rerank_evidence(items)

        items = _assign_evidence_ids(items[:min(context_top_n, max_total)], prefix="E")
        if items:
            evidence_payload = {
                "ticker": ticker,
                "method": "searxng_market_research",
                "market_type": infer_market_type(ticker),
                "languages": languages,
                "items": items,
                "article_fetch": {
                    "enabled": article_fetch_enabled,
                    "attempted": len(article_records),
                    "ok": sum(1 for a in article_records if a.get("ok")),
                },
                "calls": calls,
                "errors": errors,
            }
            ctx.evidence_file.write_text(json_dumps(evidence_payload), encoding="utf-8")
            if article_records:
                article_records = _assign_evidence_ids(article_records, prefix="A")
                ctx.articles_file.write_text(json_dumps({"ticker": ticker, "method": "fetch_article_text", "items": article_records}), encoding="utf-8")
        return json_dumps({
            "ok": bool(items),
            "method": "searxng_market_research",
            "market_type": infer_market_type(ticker),
            "languages": languages,
            "count": len(items),
            "evidence_file": str(ctx.evidence_file) if items else None,
            "articles_file": str(ctx.articles_file) if article_records else None,
            "article_fetch": {
                "enabled": article_fetch_enabled,
                "attempted": len(article_records),
                "ok": sum(1 for a in article_records if a.get("ok")),
            },
            "items": items,
            "calls": calls,
            "errors": errors[:10],
            "fallback_hint": "如果 count 太少或质量差，请调用 dashscope_market_research 作为兜底。" if len(items) < ctx.min_notes else "",
        })


class SerperSearchTool(BaseTool):
    name = "serper_search"
    description = (
        "使用 Serper API 执行单次 Google SERP 搜索，返回结构化 title/url/snippet/date。"
        "这是结构化 evidence provider，不等于 Qwen-Agent 内置 web_search；不会绕过证据审计。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "language": {"type": "string", "description": "en-US/zh-CN/auto；默认 en-US"},
            "search_type": {"type": "string", "description": "search 或 news；默认 search"},
            "max_results": {"type": "integer", "description": "最多返回结果数，默认 8"},
        },
        "required": ["query"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        p = parse_tool_params(params)
        query = str(p.get("query") or "").strip()
        if not query:
            return json_dumps({"ok": False, "errors": ["query 不能为空。"]})
        if not _serper_api_key():
            return json_dumps({"ok": False, "errors": ["SERPER_API_KEY 未配置，无法使用 serper_search。"]})
        language = str(p.get("language") or "en-US").strip()
        if language.lower() == "auto":
            language = infer_search_languages(get_context().ticker)[0]
        search_type = str(p.get("search_type") or os.environ.get("SERPER_TYPE") or "search").strip().lower()
        max_results = int(p.get("max_results") or os.environ.get("SERPER_MAX_RESULTS_PER_QUERY") or 8)
        gl, hl = _serper_gl_hl(language)
        timeout = float(os.environ.get("SERPER_TIMEOUT", "15"))
        payload = {"q": query, "gl": gl, "hl": hl, "num": max_results}
        try:
            data = _http_post_json(_serper_endpoint(search_type), payload=payload, timeout=timeout, headers={"X-API-KEY": _serper_api_key()})
            result_key = "news" if search_type == "news" else "organic"
            raw = data.get(result_key) or data.get("organic") or data.get("news") or []
            results = [_parse_serper_result(x, query=query, language=language, search_type=search_type) for x in raw[:max_results] if isinstance(x, dict)]
            return json_dumps({"ok": True, "method": "serper_search", "query": query, "language": language, "search_type": search_type, "count": len(results), "results": results})
        except Exception as exc:
            return json_dumps({"ok": False, "errors": [f"Serper 搜索失败: {exc}"]})


class SerperMarketResearchTool(BaseTool):
    name = "serper_market_research"
    description = (
        "使用 Serper API 按 ticker 自动执行多轮 Google 搜索，并输出 Serper raw/reranked/evidence 文件。"
        "用于和 SearXNG 做公平 A/B 测试；结果进入同一证据绑定流程。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "languages": {"type": "array", "items": {"type": "string"}},
            "max_results_per_query": {"type": "integer", "description": "默认 SERPER_MAX_RESULTS_PER_QUERY 或 8"},
            "max_total_results": {"type": "integer", "description": "默认 SERPER_MAX_TOTAL_RESULTS 或 40"},
        },
        "required": ["ticker"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        ticker = str(p.get("ticker") or ctx.ticker).strip().upper()
        if not _serper_api_key():
            return json_dumps({"ok": False, "errors": ["SERPER_API_KEY 未配置，无法使用 serper_market_research。"]})
        languages = p.get("languages") or infer_search_languages(ticker)
        if isinstance(languages, str):
            languages = [x.strip() for x in languages.split(",") if x.strip()]
        max_per_query = int(p.get("max_results_per_query") or os.environ.get("SERPER_MAX_RESULTS_PER_QUERY") or 8)
        max_total = int(p.get("max_total_results") or os.environ.get("SERPER_MAX_TOTAL_RESULTS") or 40)
        context_top_n = int(os.environ.get("EVIDENCE_CONTEXT_TOP_N", "18"))

        raw_items, calls, errors = _run_serper_raw_search(ctx, ticker, list(languages), max_per_query=max_per_query)
        ctx.serper_raw_results_file.write_text(json_dumps({
            "ticker": ticker,
            "method": "serper_market_research_raw",
            "raw_count": len(raw_items),
            "items": raw_items,
            "calls": calls,
            "errors": errors,
        }), encoding="utf-8")

        no_fetch = os.environ.get("SERPER_ARTICLE_FETCH_ENABLED", os.environ.get("ARTICLE_FETCH_ENABLED", "true")).strip().lower() in {"0", "false", "no"}
        items, article_records = _prepare_evidence_from_raw(ctx, ticker, raw_items, max_total=max_total, context_top_n=context_top_n, provider="serper", fetch_articles=not no_fetch)
        ctx.serper_reranked_evidence_file.write_text(json_dumps({
            "ticker": ticker,
            "method": "serper_market_research_reranked",
            "count": len(items),
            "items": items,
        }), encoding="utf-8")

        if items:
            ctx.evidence_file.write_text(json_dumps({
                "ticker": ticker,
                "method": "serper_market_research",
                "market_type": infer_market_type(ticker),
                "languages": languages,
                "items": items,
                "article_fetch": {"enabled": not no_fetch, "attempted": len(article_records), "ok": sum(1 for a in article_records if a.get("ok"))},
                "calls": calls,
                "errors": errors,
            }), encoding="utf-8")
            if article_records:
                article_records = _assign_evidence_ids(article_records, prefix="A")
                ctx.articles_file.write_text(json_dumps({"ticker": ticker, "method": "fetch_article_text", "items": article_records}), encoding="utf-8")
        ctx.search_quality_report_file.write_text(json_dumps({
            "ticker": ticker,
            "mode": "serper_only",
            "providers": {"serper_raw": _quality_metrics(raw_items, "serper_raw"), "serper_evidence": _quality_metrics(items, "serper_evidence")},
            "files": {"serper_raw": str(ctx.serper_raw_results_file), "serper_reranked": str(ctx.serper_reranked_evidence_file), "evidence": str(ctx.evidence_file), "articles": str(ctx.articles_file)},
        }), encoding="utf-8")
        return json_dumps({
            "ok": bool(items),
            "method": "serper_market_research",
            "count": len(items),
            "raw_count": len(raw_items),
            "evidence_file": str(ctx.evidence_file) if items else None,
            "serper_raw_results_file": str(ctx.serper_raw_results_file),
            "serper_reranked_evidence_file": str(ctx.serper_reranked_evidence_file),
            "search_quality_report_file": str(ctx.search_quality_report_file),
            "article_fetch": {"enabled": not no_fetch, "attempted": len(article_records), "ok": sum(1 for a in article_records if a.get("ok"))},
            "items": items,
            "calls": calls,
            "errors": errors[:10],
        })


class CombinedMarketResearchTool(BaseTool):
    name = "combined_market_research"
    description = (
        "V5.2 A/B 测试工具：按同一组 ticker 查询并行调用 SearXNG 与 Serper，"
        "输出 searxng_raw、serper_raw、combined_reranked_evidence、search_quality_report，并把 combined evidence 写入 evidence.json。"
        "SEARCH_PROVIDER=both/serper/searxng 控制启用来源。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "providers": {"type": "string", "description": "both、serper、searxng 或 auto；默认 SEARCH_PROVIDER/both"},
            "languages": {"type": "array", "items": {"type": "string"}},
            "max_results_per_query": {"type": "integer", "description": "默认 8"},
            "max_total_results": {"type": "integer", "description": "默认 60"},
        },
        "required": ["ticker"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        ticker = str(p.get("ticker") or ctx.ticker).strip().upper()
        providers = str(p.get("providers") or os.environ.get("SEARCH_PROVIDER") or "both").strip().lower()
        if providers == "auto":
            if _serper_api_key() and _searxng_base_url():
                providers = "both"
            elif _serper_api_key():
                providers = "serper"
            else:
                providers = "searxng"
        provider_set = {x.strip() for x in providers.split(",") if x.strip()}
        if "both" in provider_set:
            provider_set.update({"searxng", "serper"})
        languages = p.get("languages") or infer_search_languages(ticker)
        if isinstance(languages, str):
            languages = [x.strip() for x in languages.split(",") if x.strip()]
        max_per_query = int(p.get("max_results_per_query") or os.environ.get("SEARCH_MAX_RESULTS_PER_QUERY") or 8)
        max_total = int(p.get("max_total_results") or os.environ.get("COMBINED_MAX_TOTAL_RESULTS") or 60)
        context_top_n = int(os.environ.get("EVIDENCE_CONTEXT_TOP_N", "18"))

        all_raw: list[dict[str, Any]] = []
        provider_payloads: dict[str, Any] = {}
        errors: list[str] = []
        calls: list[dict[str, Any]] = []

        if "searxng" in provider_set:
            sx_raw, sx_calls, sx_errors = _run_searxng_raw_search(ctx, ticker, list(languages), max_per_query=max_per_query)
            ctx.raw_results_file.write_text(json_dumps({"ticker": ticker, "method": "searxng_market_research_raw", "raw_count": len(sx_raw), "items": sx_raw, "calls": sx_calls, "errors": sx_errors}), encoding="utf-8")
            sx_items = _rerank_evidence(_dedupe_evidence(sx_raw, max_items=int(os.environ.get("SEARXNG_MAX_TOTAL_RESULTS", "40"))))
            ctx.reranked_evidence_file.write_text(json_dumps({"ticker": ticker, "method": "searxng_market_research_reranked_preview", "count": len(sx_items), "items": sx_items[:int(os.environ.get("EVIDENCE_CONTEXT_TOP_N", "18"))]}), encoding="utf-8")
            all_raw.extend(sx_raw)
            calls.extend({**c, "provider": "searxng"} for c in sx_calls)
            errors.extend(sx_errors)
            provider_payloads["searxng"] = {"raw_count": len(sx_raw), "errors": sx_errors, "calls": sx_calls, "raw_file": str(ctx.raw_results_file), "reranked_file": str(ctx.reranked_evidence_file)}

        if "serper" in provider_set:
            sp_raw, sp_calls, sp_errors = _run_serper_raw_search(ctx, ticker, list(languages), max_per_query=max_per_query)
            ctx.serper_raw_results_file.write_text(json_dumps({"ticker": ticker, "method": "serper_market_research_raw", "raw_count": len(sp_raw), "items": sp_raw, "calls": sp_calls, "errors": sp_errors}), encoding="utf-8")
            sp_items = _rerank_evidence(_dedupe_evidence(sp_raw, max_items=int(os.environ.get("SERPER_MAX_TOTAL_RESULTS", "40"))))
            ctx.serper_reranked_evidence_file.write_text(json_dumps({"ticker": ticker, "method": "serper_market_research_reranked_preview", "count": len(sp_items), "items": sp_items[:int(os.environ.get("EVIDENCE_CONTEXT_TOP_N", "18"))]}), encoding="utf-8")
            all_raw.extend(sp_raw)
            calls.extend({**c, "provider": "serper"} for c in sp_calls)
            errors.extend(sp_errors)
            provider_payloads["serper"] = {"raw_count": len(sp_raw), "errors": sp_errors, "calls": sp_calls, "raw_file": str(ctx.serper_raw_results_file), "reranked_file": str(ctx.serper_reranked_evidence_file)}

        items, article_records = _prepare_evidence_from_raw(ctx, ticker, all_raw, max_total=max_total, context_top_n=context_top_n, provider="combined", fetch_articles=True)
        ctx.combined_reranked_evidence_file.write_text(json_dumps({"ticker": ticker, "method": "combined_market_research_reranked", "count": len(items), "items": items}), encoding="utf-8")

        if items:
            ctx.evidence_file.write_text(json_dumps({
                "ticker": ticker,
                "method": "combined_market_research",
                "market_type": infer_market_type(ticker),
                "languages": languages,
                "providers": sorted(provider_set),
                "items": items,
                "article_fetch": {"enabled": True, "attempted": len(article_records), "ok": sum(1 for a in article_records if a.get("ok"))},
                "calls": calls,
                "errors": errors,
            }), encoding="utf-8")
            if article_records:
                article_records = _assign_evidence_ids(article_records, prefix="A")
                ctx.articles_file.write_text(json_dumps({"ticker": ticker, "method": "fetch_article_text", "items": article_records}), encoding="utf-8")

        provider_counts: dict[str, int] = {}
        for item in items:
            provider = str(item.get("provider") or item.get("engine") or "unknown")
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
        quality_report = {
            "ticker": ticker,
            "mode": "combined_ab_test",
            "providers_requested": sorted(provider_set),
            "searxng_config": {
                "engines": os.environ.get("SEARXNG_ENGINES", "") or "default",
                "categories": os.environ.get("SEARXNG_CATEGORIES", "general,news"),
                "time_range": os.environ.get("SEARXNG_TIME_RANGE", "month"),
                "note": "To test Google-backed SearXNG, set SEARXNG_ENGINES=google or 'google,google news'.",
            },
            "provider_payloads": provider_payloads,
            "metrics": {
                "combined_raw": _quality_metrics(all_raw, "combined_raw"),
                "combined_final_evidence": _quality_metrics(items, "combined_final_evidence"),
                "articles": _quality_metrics(article_records, "articles"),
                "final_evidence_provider_counts": provider_counts,
            },
            "files": {
                "searxng_raw": str(ctx.raw_results_file),
                "serper_raw": str(ctx.serper_raw_results_file),
                "combined_reranked": str(ctx.combined_reranked_evidence_file),
                "evidence": str(ctx.evidence_file),
                "articles": str(ctx.articles_file),
            },
            "notes": "Use final_notes.json after save_news_notes to see which provider actually contributed to the report.",
        }
        ctx.search_quality_report_file.write_text(json_dumps(quality_report), encoding="utf-8")
        return json_dumps({
            "ok": bool(items),
            "method": "combined_market_research",
            "providers": sorted(provider_set),
            "raw_count": len(all_raw),
            "count": len(items),
            "evidence_file": str(ctx.evidence_file) if items else None,
            "articles_file": str(ctx.articles_file) if article_records else None,
            "combined_reranked_evidence_file": str(ctx.combined_reranked_evidence_file),
            "search_quality_report_file": str(ctx.search_quality_report_file),
            "article_fetch": {"enabled": True, "attempted": len(article_records), "ok": sum(1 for a in article_records if a.get("ok"))},
            "provider_counts_in_final_evidence": provider_counts,
            "items": items,
            "errors": errors[:12],
        })


class PriorityMarketResearchTool(BaseTool):
    name = "priority_market_research"
    description = (
        "V5.4 生产版检索入口：优先使用 Serper 结构化搜索；只有 Serper evidence 不足时才调用 DashScope enable_search+enable_source；"
        "若仍不足，再退化到 SearXNG。主 Agent 自身 web_search 应保持关闭。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "languages": {"type": "array", "items": {"type": "string"}},
            "max_results_per_query": {"type": "integer", "description": "默认 8"},
            "max_total_results": {"type": "integer", "description": "默认 60"},
            "force_dashscope": {"type": "boolean", "description": "即使 Serper 足够也调用 DashScope 补充；默认 false"},
            "force_searxng": {"type": "boolean", "description": "即使 Serper/DashScope 足够也调用 SearXNG；默认 false"},
        },
        "required": ["ticker"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        ticker = str(p.get("ticker") or ctx.ticker).strip().upper()
        languages = p.get("languages") or infer_search_languages(ticker)
        if isinstance(languages, str):
            languages = [x.strip() for x in languages.split(",") if x.strip()]
        max_per_query = int(p.get("max_results_per_query") or os.environ.get("SEARCH_MAX_RESULTS_PER_QUERY") or os.environ.get("SERPER_MAX_RESULTS_PER_QUERY") or 8)
        max_total = int(p.get("max_total_results") or os.environ.get("PRIORITY_MAX_TOTAL_RESULTS") or os.environ.get("COMBINED_MAX_TOTAL_RESULTS") or 60)
        context_top_n = int(os.environ.get("EVIDENCE_CONTEXT_TOP_N", "18"))
        fetch_articles = os.environ.get("ARTICLE_FETCH_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
        force_dashscope = bool(p.get("force_dashscope", False)) or os.environ.get("FORCE_DASHSCOPE_AFTER_SERPER", "false").strip().lower() in {"1", "true", "yes"}
        force_searxng = bool(p.get("force_searxng", False)) or os.environ.get("FORCE_SEARXNG_FALLBACK", "false").strip().lower() in {"1", "true", "yes"}

        all_raw: list[dict[str, Any]] = []
        errors: list[str] = []
        calls: list[dict[str, Any]] = []
        provider_payloads: dict[str, Any] = {}
        fallback_triggers: list[str] = []
        article_records: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        serper_sufficiency: dict[str, Any] = {"ok": False, "reason": "Serper not attempted"}
        dashscope_result: dict[str, Any] = {"called": False}
        searxng_result: dict[str, Any] = {"called": False}

        # 1) Serper first.
        if _serper_api_key():
            sp_raw, sp_calls, sp_errors = _run_serper_raw_search(ctx, ticker, list(languages), max_per_query=max_per_query)
            ctx.serper_raw_results_file.write_text(json_dumps({"ticker": ticker, "method": "serper_market_research_raw", "raw_count": len(sp_raw), "items": sp_raw, "calls": sp_calls, "errors": sp_errors}), encoding="utf-8")
            sp_items, sp_article_records = _prepare_evidence_from_raw(ctx, ticker, sp_raw, max_total=int(os.environ.get("SERPER_MAX_TOTAL_RESULTS", "40")), context_top_n=context_top_n, provider="serper", fetch_articles=fetch_articles)
            ctx.serper_reranked_evidence_file.write_text(json_dumps({"ticker": ticker, "method": "serper_market_research_reranked", "count": len(sp_items), "items": sp_items}), encoding="utf-8")
            all_raw.extend(sp_raw)
            items = sp_items
            article_records.extend(sp_article_records)
            calls.extend({**c, "provider": "serper"} for c in sp_calls)
            errors.extend(sp_errors)
            provider_payloads["serper"] = {"raw_count": len(sp_raw), "errors": sp_errors, "calls": sp_calls, "raw_file": str(ctx.serper_raw_results_file), "reranked_file": str(ctx.serper_reranked_evidence_file)}
            current_data = _load_current_data(ctx)
            required_focus = _required_focus_coverage(ticker, current_data)
            serper_sufficiency = _evaluate_evidence_sufficiency(sp_items, required_focus=required_focus)
        else:
            errors.append("SERPER_API_KEY 未配置，跳过第一优先级 Serper。")
            fallback_triggers.append("serper_missing_key")

        # 2) DashScope source search if Serper insufficient or forced.
        dashscope_called = False
        if force_dashscope or not serper_sufficiency.get("ok"):
            if os.environ.get("DASHSCOPE_API_KEY"):
                dashscope_called = True
                fallback_triggers.append("force_dashscope" if force_dashscope else f"serper_insufficient:{serper_sufficiency.get('reason')}")
                try:
                    ds_json = DashScopeMarketResearchTool().call({"ticker": ticker, "languages": languages, "target_count": int(os.environ.get("DASHSCOPE_TARGET_COUNT", "8"))})
                    dashscope_result = json.loads(ds_json)
                except Exception as exc:
                    dashscope_result = {"ok": False, "called": True, "errors": [str(exc)]}
                dashscope_result["called"] = True
                ds_payload = _load_json_file(ctx.dashscope_sources_file)
                ds_items = ds_payload.get("items", []) if isinstance(ds_payload.get("items"), list) else []
                provider_payloads["dashscope"] = {"called": True, "source_count": len(ds_items), "sources_file": str(ctx.dashscope_sources_file), "ok": bool(dashscope_result.get("ok")), "errors": dashscope_result.get("errors", [])}
            else:
                errors.append("DASHSCOPE_API_KEY 未配置，无法执行第二优先级 DashScope source fallback。")
                fallback_triggers.append("dashscope_missing_key")

        # 3) SearXNG only as final fallback, based on combined verifiable count.
        records_now = _collect_verifiable_records(ctx)
        min_after_fallback = int(os.environ.get("MIN_VERIFIABLE_RECORDS_BEFORE_SEARXNG", "10"))
        need_searxng = force_searxng or (len(records_now) + len(items) < min_after_fallback and not serper_sufficiency.get("ok"))
        if need_searxng:
            if _searxng_base_url():
                sx_raw, sx_calls, sx_errors = _run_searxng_raw_search(ctx, ticker, list(languages), max_per_query=max_per_query)
                ctx.raw_results_file.write_text(json_dumps({"ticker": ticker, "method": "searxng_market_research_raw", "raw_count": len(sx_raw), "items": sx_raw, "calls": sx_calls, "errors": sx_errors}), encoding="utf-8")
                all_raw.extend(sx_raw)
                calls.extend({**c, "provider": "searxng"} for c in sx_calls)
                errors.extend(sx_errors)
                searxng_result = {"called": True, "raw_count": len(sx_raw), "errors": sx_errors, "raw_file": str(ctx.raw_results_file)}
                provider_payloads["searxng"] = {"raw_count": len(sx_raw), "errors": sx_errors, "calls": sx_calls, "raw_file": str(ctx.raw_results_file), "reranked_file": str(ctx.reranked_evidence_file)}
                # Rebuild evidence from Serper+SearXNG raw only; DashScope source objects remain in dashscope_sources.json.
                items, article_records = _prepare_evidence_from_raw(ctx, ticker, all_raw, max_total=max_total, context_top_n=context_top_n, provider="priority_combined", fetch_articles=fetch_articles)
                sx_preview = _rerank_evidence(_dedupe_evidence(sx_raw, max_items=int(os.environ.get("SEARXNG_MAX_TOTAL_RESULTS", "40"))))
                ctx.reranked_evidence_file.write_text(json_dumps({"ticker": ticker, "method": "searxng_market_research_reranked_preview", "count": len(sx_preview), "items": sx_preview[:context_top_n]}), encoding="utf-8")
            else:
                searxng_result = {"called": False, "reason": "SEARXNG_URL missing"}
                errors.append("SEARXNG_URL 未配置，无法执行第三优先级 SearXNG fallback。")
        else:
            searxng_result = {"called": False, "reason": "not_needed"}

        items = _assign_evidence_ids(items, prefix="E")
        ctx.combined_reranked_evidence_file.write_text(json_dumps({"ticker": ticker, "method": "priority_market_research_reranked", "count": len(items), "items": items}), encoding="utf-8")
        if items:
            ctx.evidence_file.write_text(json_dumps({
                "ticker": ticker,
                "method": "priority_market_research",
                "market_type": infer_market_type(ticker),
                "languages": languages,
                "provider_priority": os.environ.get("SEARCH_PROVIDER_PRIORITY", "serper,dashscope,searxng"),
                "items": items,
                "article_fetch": {"enabled": fetch_articles, "attempted": len(article_records), "ok": sum(1 for a in article_records if a.get("ok")), "quality_ok": sum(1 for a in article_records if a.get("article_text_quality_ok"))},
                "calls": calls,
                "errors": errors,
            }), encoding="utf-8")
        if article_records:
            article_records = _assign_evidence_ids(article_records, prefix="A")
            ctx.articles_file.write_text(json_dumps({"ticker": ticker, "method": "fetch_article_text", "items": article_records}), encoding="utf-8")

        provider_counts: dict[str, int] = {}
        for item in items:
            provider = str(item.get("provider") or item.get("engine") or "unknown")
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
        quality_report = {
            "ticker": ticker,
            "mode": "priority_production",
            "provider_priority": os.environ.get("SEARCH_PROVIDER_PRIORITY", "serper,dashscope,searxng"),
            "provider_trigger_reason": fallback_triggers,
            "serper_sufficient": serper_sufficiency,
            "dashscope_called": dashscope_called,
            "dashscope_result": {k: v for k, v in dashscope_result.items() if k not in {"items"}},
            "searxng_called": bool(searxng_result.get("called")),
            "searxng_result": searxng_result,
            "provider_payloads": provider_payloads,
            "metrics": {
                "raw": _quality_metrics(all_raw, "raw"),
                "final_evidence": _quality_metrics(items, "final_evidence"),
                "articles": _quality_metrics(article_records, "articles"),
                "final_evidence_provider_counts": provider_counts,
            },
            "files": {
                "serper_raw": str(ctx.serper_raw_results_file),
                "dashscope_sources": str(ctx.dashscope_sources_file),
                "searxng_raw": str(ctx.raw_results_file),
                "combined_reranked": str(ctx.combined_reranked_evidence_file),
                "evidence": str(ctx.evidence_file),
                "articles": str(ctx.articles_file),
            },
        }
        ctx.search_quality_report_file.write_text(json_dumps(quality_report), encoding="utf-8")
        return json_dumps({
            "ok": bool(items) or bool(_load_json_file(ctx.dashscope_sources_file).get("items")),
            "method": "priority_market_research",
            "provider_priority": os.environ.get("SEARCH_PROVIDER_PRIORITY", "serper,dashscope,searxng"),
            "serper_sufficient": serper_sufficiency,
            "dashscope_called": dashscope_called,
            "searxng_called": bool(searxng_result.get("called")),
            "count": len(items),
            "raw_count": len(all_raw),
            "evidence_file": str(ctx.evidence_file) if items else None,
            "dashscope_sources_file": str(ctx.dashscope_sources_file),
            "combined_reranked_evidence_file": str(ctx.combined_reranked_evidence_file),
            "search_quality_report_file": str(ctx.search_quality_report_file),
            "article_fetch": {"enabled": fetch_articles, "attempted": len(article_records), "ok": sum(1 for a in article_records if a.get("ok")), "quality_ok": sum(1 for a in article_records if a.get("article_text_quality_ok"))},
            "provider_counts_in_final_evidence": provider_counts,
            "items": items,
            "errors": errors[:12],
        })


class SearchQualityReportTool(BaseTool):
    name = "inspect_search_quality_report"
    description = "读取 v5.2 搜索质量报告，比较 SearXNG 与 Serper 的 raw/reranked/final evidence 贡献。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_context()
        if not ctx.search_quality_report_file.exists():
            return json_dumps({"ok": False, "errors": ["search_quality_report_file 尚不存在，请先调用 combined_market_research 或 serper_market_research。"]})
        return ctx.search_quality_report_file.read_text(encoding="utf-8")


class FetchArticleTextTool(BaseTool):
    name = "fetch_article_text"
    description = (
        "打开一个或多个 URL，抓取网页正文/标题/meta description/发布日期。"
        "用于把 SearXNG 搜索结果从 title/snippet 升级为接近 WebFetch 的正文证据。"
        "优先用于公司 IR、SEC、Reuters、CNBC、Yahoo Finance、MarketWatch、Nasdaq 等高价值来源。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "urls": {"type": "array", "items": {"type": "string"}},
            "max_chars": {"type": "integer", "description": "每篇最多返回正文字符数，默认 5000"},
            "timeout": {"type": "number", "description": "单篇超时秒数，默认 12"},
        },
        "required": ["urls"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        urls = p.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not urls:
            return json_dumps({"ok": False, "errors": ["urls 不能为空。"]})
        max_chars = int(p.get("max_chars") or os.environ.get("ARTICLE_FETCH_MAX_CHARS") or 5000)
        timeout = float(p.get("timeout") or os.environ.get("ARTICLE_FETCH_TIMEOUT") or 12)
        max_urls = int(os.environ.get("ARTICLE_FETCH_MANUAL_MAX_URLS", "8"))

        records: list[dict[str, Any]] = []
        for url in [str(u).strip() for u in urls if str(u).strip()][:max_urls]:
            try:
                records.append(_fetch_article_text(url, timeout=timeout, max_chars=max_chars))
            except Exception as exc:
                records.append({"url": url, "ok": False, "error": str(exc), "source_domain": _source_domain(url)})

        records = _assign_evidence_ids(records, prefix="A")
        ctx.articles_file.write_text(json_dumps({"ticker": ctx.ticker, "method": "fetch_article_text", "items": records}), encoding="utf-8")
        return json_dumps({
            "ok": any(r.get("ok") for r in records),
            "count": len(records),
            "ok_count": sum(1 for r in records if r.get("ok")),
            "articles_file": str(ctx.articles_file),
            "items": records,
        })


class GenerateTechnicalNoteItemsTool(BaseTool):
    name = "generate_technical_note_items"
    description = (
        "基于 fetch_technical_data 生成的真实技术指标，自动生成 2-3 条结构化技术面 notes。"
        "该工具由 Python 固定模板生成，避免模型写错均线大小关系或 RSI/MACD 数值。"
        "返回的 items 可以直接合并进 save_news_notes 的 items 数组。"
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_context()
        d = _load_current_data(ctx)
        if not d:
            return json_dumps({"ok": False, "errors": ["缺少 data_file，请先调用 fetch_technical_data。"]})
        ticker = str(d.get("TICKER") or ctx.ticker)
        last = float(d.get("LAST_CLOSE", 0) or 0)
        pct = float(d.get("PCT", 0) or 0)
        currency = str(d.get("CURRENCY") or "USD")
        bull_count = int(d.get("bull_ma_count", 0) or 0)
        ma5 = float(d.get("ma5", 0) or 0)
        ma20 = float(d.get("ma20", 0) or 0)
        ma50 = float(d.get("ma50", 0) or 0)
        ma200 = float(d.get("ma200", 0) or 0)
        rsi = float(d.get("rsi", 0) or 0)
        macd = float(d.get("macd_line", 0) or 0)
        sig = float(d.get("signal_line", 0) or 0)
        hist = float(d.get("hist_val", 0) or 0)
        k = float(d.get("k_val", 0) or 0)
        dd = float(d.get("d_val", 0) or 0)
        j = float(d.get("j_val", 0) or 0)
        bb_pct = float(d.get("bb_pct", 0) or 0)
        vol_ratio = float(d.get("vol_ratio", 0) or 0)
        atr = float(d.get("atr14", 0) or 0)

        items: list[dict[str, str]] = []
        ma_relation = f"MA5={ma5:.2f}，MA20={ma20:.2f}，MA50={ma50:.2f}，MA200={ma200:.2f}"
        if bull_count <= 1:
            tag = "BEAR"
            title = "技术面空头结构"
            logic = "价格低于绝大多数关键均线，说明中短期趋势仍处在下行或弱势修复阶段，反弹前通常需要先收复 MA5/MA20 并看到成交量配合。"
            meaning = "短线不宜仅因估值或目标价空间追高，适合等待均线修复、MACD柱收敛或放量企稳后再提高仓位。"
        elif bull_count >= 5:
            tag = "BULL"
            title = "技术面多头排列"
            logic = "价格站上多数关键均线，说明趋势资金仍占优，回调若能守住 MA20/MA50，通常更偏向上升趋势中的整理。"
            meaning = "持仓者可继续观察趋势延续，新增仓位则需结合 RSI、布林带位置和成交量避免追高。"
        else:
            tag = "MIX"
            title = "技术面信号分化"
            logic = "均线多头数量处于中间状态，说明趋势没有形成单边确认，短线更容易受财报、评级和宏观消息驱动。"
            meaning = "适合采用分批和条件触发策略，而不是一次性判断趋势反转或趋势延续。"
        items.append({
            "tag": tag,
            "title": title,
            "fact": f"{ticker} 最新收盘价 {last:.2f} {currency}，当日涨跌幅 {pct:+.2f}%；均线多头数 {bull_count}/6，{ma_relation}。",
            "logic": logic,
            "investment_meaning": meaning,
            "source": "fetch_and_calc.py / yfinance 技术指标",
            "source_date": str(d.get("data_end") or ctx.report_date),
            "url": "",
            "evidence_id": "TECH-001",
        })

        if rsi < 30 or (k < 20 and dd < 20) or bb_pct < 25:
            items.append({
                "tag": "MIX",
                "title": "超卖修复与趋势弱势并存",
                "fact": f"RSI(14)={rsi:.1f}，KDJ 为 K={k:.1f}/D={dd:.1f}/J={j:.1f}，布林带位置 {bb_pct:.1f}%，MACD DIF={macd:.3f}/DEA={sig:.3f}/Hist={hist:.3f}，成交量比 {vol_ratio:.2f}x。",
                "logic": "RSI/KDJ 接近或进入超卖区，意味着短线抛压可能阶段性释放，但 MACD 若仍处空头区间，通常只能说明存在技术反弹概率，而不能单独确认趋势反转。",
                "investment_meaning": "激进交易者可关注止跌反抽信号，稳健投资者应等待 MACD 柱改善、价格重新站上短期均线或消息面催化配合。",
                "source": "fetch_and_calc.py / yfinance 技术指标",
                "source_date": str(d.get("data_end") or ctx.report_date),
                "url": "",
                "evidence_id": "TECH-002",
            })
        elif rsi > 70 or bb_pct > 80:
            items.append({
                "tag": "BEAR",
                "title": "短线过热风险",
                "fact": f"RSI(14)={rsi:.1f}，布林带位置 {bb_pct:.1f}%，ATR(14)={atr:.2f}，成交量比 {vol_ratio:.2f}x。",
                "logic": "动量指标处于偏热位置时，若成交量不能继续放大，股价容易出现获利回吐或横盘消化估值。",
                "investment_meaning": "短线追涨需要更严格止损，已持仓者可关注上轨附近的量价背离和回撤风险。",
                "source": "fetch_and_calc.py / yfinance 技术指标",
                "source_date": str(d.get("data_end") or ctx.report_date),
                "url": "",
                "evidence_id": "TECH-002",
            })
        chip = d.get("chip_profile_primary") or {}
        if isinstance(chip, dict) and chip.get("ok"):
            poc = float(chip.get("poc_price", 0) or 0)
            dist = float(chip.get("poc_distance_pct", 0) or 0)
            va_low = float(chip.get("value_area_low", 0) or 0)
            va_high = float(chip.get("value_area_high", 0) or 0)
            overhead = float(chip.get("overhead_supply_ratio", 0) or 0) * 100
            support = float(chip.get("support_volume_ratio", 0) or 0) * 100
            chip_score = float(chip.get("chip_score", 50) or 50)
            signal = str(chip.get("chip_signal") or "MIX_BALANCE_AREA")
            peaks = chip.get("top_peaks") or []
            peak_txt = "；".join([
                f"{float(x.get('price', 0) or 0):.2f}({x.get('role','')}, 距现价{float(x.get('distance_pct', 0) or 0):+.1f}%)"
                for x in peaks[:3] if isinstance(x, dict)
            ])
            if "BEAR" in signal:
                chip_tag = "BEAR"
                chip_title = "筹码峰显示上方套牢压力偏重"
                chip_logic = "当前价位于主要成交密集区下方或下沿，上方历史成交量占比较高，反弹过程中容易遇到解套盘或获利盘压力。"
                chip_meaning = "若价格无法放量站上 POC 和价值区间上沿，技术反弹可能受限；突破 POC 后再观察是否能转为支撑。"
            elif "BULL" in signal:
                chip_tag = "BULL"
                chip_title = "筹码峰显示下方成本支撑较强"
                chip_logic = "当前价位于主要成交密集区上方或价值区间上半部，下方累计成交量占比较高，说明成本沉淀可能形成支撑。"
                chip_meaning = "若回撤不跌破 POC 或价值区间下沿，技术面更容易形成震荡上行或趋势修复。"
            else:
                chip_tag = "MIX"
                chip_title = "筹码峰处于平衡博弈区"
                chip_logic = "当前价接近主要成交密集区，说明多空成本区重叠，方向性不如均线突破或放量跌破更明确。"
                chip_meaning = "适合把 POC、价值区间上下沿作为观察位，等待价格离开平衡区后再提高方向判断置信度。"
            items.append({
                "tag": chip_tag,
                "title": chip_title,
                "fact": f"126日筹码峰近似计算显示：POC={poc:.2f} {currency}，当前价距 POC {dist:+.2f}%；70%价值区间约 {va_low:.2f}-{va_high:.2f}；上方筹码占比 {overhead:.1f}%，下方支撑筹码占比 {support:.1f}%，chip_score={chip_score:.1f}/100。主要峰值：{peak_txt}。",
                "logic": chip_logic,
                "investment_meaning": chip_meaning,
                "source": "fetch_and_calc.py / yfinance 日线 OHLCV 筹码峰近似",
                "source_date": str(d.get("data_end") or ctx.report_date),
                "url": "",
                "evidence_id": "TECH-003",
            })

        return json_dumps({"ok": True, "count": len(items), "items": items})


class DashScopeMarketResearchTool(BaseTool):
    name = "dashscope_market_research"
    description = (
        "使用 DashScope/Qwen 模型自带联网搜索能力，为指定 ticker 收集市场消息面线索。"
        "V5.1 使用 DashScope 协议 enable_source=true 保存真实搜索来源到 dashscope_sources.json。"
        "模型生成的 items 仍是 candidate；只有 DS-xxx source_id 对应的来源对象可作为 final notes 证据。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "technical_summary": {"type": "string", "description": "fetch_technical_data 返回的技术面摘要，可为空"},
            "focus": {
                "type": "array",
                "items": {"type": "string"},
                "description": "需要覆盖的研究方向，例如 earnings、analyst ratings、industry、macro、risks。",
            },
            "min_items": {"type": "integer", "description": "希望返回的证据条数，默认 12"},
        },
        "required": ["ticker"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        ticker = str(p.get("ticker") or ctx.ticker).strip().upper()
        technical_summary = str(p.get("technical_summary") or "").strip()
        focus = p.get("focus") or [
            "latest earnings and guidance",
            "analyst ratings and target price changes",
            "industry and competitor news",
            "macro policy and rates",
            "major events, cloud/AI/product/regulation",
            "risks, valuation, sentiment and flows",
        ]
        min_items = int(p.get("min_items") or max(ctx.min_notes, 12))

        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if not api_key:
            return json_dumps({
                "ok": False,
                "errors": ["DASHSCOPE_API_KEY 未配置，无法使用 dashscope_market_research。"],
            })

        model = os.environ.get("DASHSCOPE_RESEARCH_MODEL") or os.environ.get("QWEN_RESEARCH_MODEL") or "qwen-plus"

        system = (
            "你是金融市场证据检索助手。请使用联网搜索能力收集最新、可核验的市场信息。"
            "只输出严格 JSON，不要输出 markdown。每条 item 的 url 必须来自可核验搜索来源；"
            "如果无法确认来源，不要写入 items。"
        )
        user = f"""
请为 {ticker} 收集股票/ETF/加密货币日报所需的消息面证据。当前报告日期：{ctx.report_date}。

技术面摘要（如有）：
{technical_summary or '未提供'}

研究方向：
{json_dumps(focus)}

要求：
- 至少返回 {min_items} 条 evidence items。
- 覆盖财报/业绩、分析师评级/目标价、行业动态、宏观环境、重大事件、多空风险、市场情绪/资金流向。
- 每条必须尽量包含 source、source_date、url、facts、relevance、sentiment_hint。
- sentiment_hint 只能是 BULL、BEAR 或 MIX。
- 不要编造无法核验的数据；如果信息不确定，在 facts 中说明不确定性。

严格输出 JSON：
{{
  "items": [
    {{
      "title": "...",
      "source": "...",
      "source_date": "YYYY-MM-DD 或 unknown",
      "url": "...",
      "facts": "包含具体数据/事件/时间",
      "relevance": "为什么影响 {ticker}",
      "sentiment_hint": "BULL|BEAR|MIX"
    }}
  ]
}}
""".strip()
        try:
            import dashscope
            dashscope.base_http_api_url = os.environ.get("DASHSCOPE_API_BASE", "https://dashscope.aliyuncs.com/api/v1")
            strategy = os.environ.get("DASHSCOPE_SEARCH_STRATEGY", "max")
            search_options: dict[str, Any] = {
                "search_strategy": strategy,
                "enable_source": True,
                "enable_citation": True,
                "citation_format": "[ref_<number>]",
            }
            freshness = os.environ.get("DASHSCOPE_SEARCH_FRESHNESS", "30").strip()
            if freshness and strategy == "turbo":
                search_options["freshness"] = int(freshness)

            response = dashscope.Generation.call(
                api_key=api_key,
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                enable_search=True,
                search_options=search_options,
                result_format="message",
            )
            raw, raw_sources, meta = _dashscope_content_and_sources(response)
            source_records = [_dashscope_source_record(src, i, model=model) for i, src in enumerate(raw_sources, start=1)]
            if source_records:
                ctx.dashscope_sources_file.write_text(json_dumps({
                    "ticker": ticker,
                    "method": "dashscope_search_source",
                    "model": model,
                    "search_strategy": strategy,
                    "count": len(source_records),
                    "items": source_records,
                    "meta": meta,
                    "note": "These DS-xxx records are verifiable DashScope source objects and may be cited by final notes when ALLOW_DASHSCOPE_SOURCES_IN_NOTES=true.",
                }), encoding="utf-8")

            payload = _extract_json_payload(raw)
            if payload and isinstance(payload.get("items"), list):
                items = payload["items"]
                for i, item in enumerate(items, start=1):
                    if isinstance(item, dict):
                        matched_id = _match_dashscope_source_id(item, source_records)
                        if matched_id:
                            item.setdefault("evidence_id", matched_id)
                        else:
                            item.setdefault("evidence_id", f"D{i:03d}")
                        item["candidate_only"] = True
                        item["evidence_method"] = "dashscope_market_research_candidate"
                ctx.candidates_file.write_text(json_dumps({
                    "ticker": ticker,
                    "items": items,
                    "method": "dashscope_market_research",
                    "model": model,
                    "candidate_only": True,
                    "dashscope_sources_file": str(ctx.dashscope_sources_file) if source_records else None,
                    "note": "Model-generated candidates are not final evidence unless they cite DS-xxx sources from dashscope_sources.json.",
                }), encoding="utf-8")
                return json_dumps({
                    "ok": True,
                    "method": "dashscope_market_research",
                    "model": model,
                    "candidate_only": True,
                    "sources_count": len(source_records),
                    "dashscope_sources_file": str(ctx.dashscope_sources_file) if source_records else None,
                    "count": len(items),
                    "candidates_file": str(ctx.candidates_file),
                    "dashscope_sources_file": str(ctx.dashscope_sources_file),
                    "items": items,
                    "source_items": source_records[:20],
                    "warning": "DashScope model items are candidates; final notes may cite DS-xxx only when the source exists in dashscope_sources.json.",
                })
            return json_dumps({
                "ok": bool(source_records),
                "method": "dashscope_market_research",
                "model": model,
                "sources_count": len(source_records),
                "dashscope_sources_file": str(ctx.dashscope_sources_file) if source_records else None,
                "source_items": source_records[:20],
                "errors": [] if source_records else ["DashScope 未返回可解析 JSON，也没有返回 search sources。"],
                "raw_output": raw[:6000],
            })
        except Exception as exc:
            return json_dumps({"ok": False, "errors": [f"dashscope_market_research 调用失败: {exc}"]})


class SaveEvidenceTool(BaseTool):
    name = "save_market_evidence"
    description = "保存 agent 通过 SearXNG、web_search/web_extractor 或 DashScope 收集到的事实证据。用于审计 notes 来源，不会直接写入 HTML。"
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "证据列表，每项包含 title/source/date/url/facts/relevance/sentiment_hint。",
                "items": {"type": "object"},
            },
            "method": {"type": "string", "description": "证据来源方法，例如 searxng_market_research、web_search、dashscope_market_research"},
        },
        "required": ["items"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        items = p.get("items", [])
        method = str(p.get("method") or "manual_save_market_evidence")
        if not isinstance(items, list) or not items:
            return json_dumps({"ok": False, "errors": ["items 不能为空，且必须是数组。"]})
        items = _assign_evidence_ids(items, prefix="E")
        ctx.evidence_file.write_text(json_dumps({"ticker": ctx.ticker, "method": method, "items": items}), encoding="utf-8")
        return json_dumps({"ok": True, "evidence_file": str(ctx.evidence_file), "count": len(items), "method": method, "items": items})


def _clamp_score(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return 50.0


def _rating_label(score: float, instrument_type: str = "EQUITY") -> tuple[str, str]:
    itype = str(instrument_type or "EQUITY").upper()
    if score >= 75:
        labels = {
            "INDEX": "积极看多 OVERWEIGHT",
            "ETF": "积极增配 OVERWEIGHT",
            "CRYPTO": "偏强看多 BULLISH",
        }
        return labels.get(itype, "积极买入 BUY"), "buy"
    if score >= 65:
        labels = {
            "INDEX": "偏多持有 / 回调增配",
            "ETF": "适度增配 ACCUMULATE",
            "CRYPTO": "偏多观察 / 控制仓位",
        }
        return labels.get(itype, "逢低布局 ACCUMULATE"), "buy"
    if score >= 50:
        return "中性持有 HOLD", "hold"
    if score >= 40:
        return "观察等待 WATCH", "hold"
    return "暂不参与 AVOID", "avoid"



def _safe_float_value(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _score_status(used: dict[str, Any], missing: list[str], weak_when_lt: int = 2) -> str:
    real_used = {k: v for k, v in used.items() if v not in {None, "", 0, 0.0, "N/A"}}
    if len(real_used) == 0:
        return "WEAK"
    if len(real_used) < weak_when_lt or len(missing) >= 4:
        return "PARTIAL"
    if missing:
        return "PARTIAL"
    return "FULL"


def _score_by_ranges(value: float, ranges: list[tuple[float, float]]) -> float:
    """Return the score for the first range where value <= upper_bound."""
    for upper, score in ranges:
        if value <= upper:
            return score
    return ranges[-1][1] if ranges else 50.0


def _weighted_average(parts: list[tuple[float | None, float]]) -> float | None:
    total = 0.0
    weight = 0.0
    for val, w in parts:
        if val is None:
            continue
        total += float(val) * float(w)
        weight += float(w)
    if weight <= 0:
        return None
    return total / weight


def _analyst_rating_text_score(text: str) -> float | None:
    t = str(text or "").strip().lower()
    if not t:
        return None
    # StockAnalysis commonly returns strings such as "Buy", "Strong Buy", "Hold".
    if "strong buy" in t:
        return 92.0
    if "buy" in t or "outperform" in t or "overweight" in t:
        return 82.0
    if "hold" in t or "neutral" in t or "market perform" in t:
        return 55.0
    if "underperform" in t or "underweight" in t:
        return 35.0
    if "sell" in t:
        return 25.0
    return 50.0


def _compute_final_rating_payload(ctx: RunContext, notes: list[Any]) -> dict[str, Any]:
    """Compute v5.8 instrument-aware, auditable multi-factor rating.

    Applicable models:
    - EQUITY: technical + news + valuation + analyst + risk
    - ETF: technical + news + ETF valuation + risk; analyst is NOT_APPLICABLE
    - INDEX/CRYPTO/OTHER: technical + news + risk; valuation/analyst are NOT_APPLICABLE

    Missing but applicable scores are excluded and remaining effective weights are
    renormalized. NOT_APPLICABLE is never represented as an artificial neutral 50.
    """
    d = _load_current_data(ctx) or {}
    sources = d.get("FUNDAMENTAL_SOURCES") or {}
    instrument_type = str(d.get("INSTRUMENT_TYPE") or "EQUITY").upper()
    scoring_profile = str(d.get("SCORING_PROFILE") or "equity_five_factor")

    # --- Technical score: deterministic Python score from OHLCV; unavailable volume/chip components were already reweighted. ---
    technical_raw = d.get("technical_score")
    technical_score = _clamp_score(float(technical_raw)) if technical_raw is not None else None
    technical_used = {
        "technical_score": technical_raw,
        "technical_signal": d.get("technical_signal"),
        "technical_subscores": d.get("technical_subscores"),
        "technical_effective_weights": d.get("technical_effective_weights"),
        "technical_unavailable_components": d.get("technical_unavailable_components"),
        "chip_profile_primary": d.get("chip_profile_primary"),
    }

    # --- News score: note balance + evidence quality. Model-note-derived, therefore PARTIAL by design. ---
    non_technical_notes = [n for n in notes if not getattr(n, "is_technical", False)]
    counts = {tag: sum(1 for n in non_technical_notes if getattr(n, "tag", "") == tag) for tag in ["BULL", "BEAR", "MIX"]}
    ab_notes = sum(1 for n in non_technical_notes if str(getattr(n, "evidence_grade", "")).upper() in {"A", "B"})
    non_tech_count = len(non_technical_notes)
    quality_bonus = min(10.0, ab_notes / max(1, non_tech_count) * 10.0)
    news_score = _clamp_score(50 + (counts["BULL"] - counts["BEAR"]) * 7 + counts["MIX"] * 1.5 + quality_bonus) if non_technical_notes else None

    # --- Valuation: StockAnalysis-first multiples. No peer comparison or historical percentile in v5.8. ---
    valuation_applicable = instrument_type in {"EQUITY", "ETF"}
    valuation_inputs_used: dict[str, Any] = {}
    valuation_missing: list[str] = []
    valuation_parts: list[tuple[float | None, float]] = []

    def add_positive(name: str, key: str, weight: float, ranges: list[tuple[float, float]]) -> None:
        value = _safe_float_value(d.get(key), 0.0)
        if value > 0:
            valuation_inputs_used[name] = value
            valuation_parts.append((_score_by_ranges(value, ranges), weight))
        else:
            valuation_missing.append(name)

    if valuation_applicable:
        add_positive("forward_pe", "FW_PE", 0.22, [(10, 96), (15, 88), (20, 78), (25, 68), (35, 55), (50, 40), (10**9, 25)])
        add_positive("trailing_pe", "TTM_PE", 0.10, [(12, 92), (18, 82), (25, 70), (35, 58), (50, 43), (10**9, 28)])
        add_positive("peg_ratio", "PEG_RATIO", 0.20, [(1.0, 94), (1.5, 82), (2.0, 70), (3.0, 55), (5.0, 38), (10**9, 25)])
        add_positive("ev_sales", "EV_SALES", 0.14, [(3, 88), (6, 75), (10, 60), (15, 45), (10**9, 30)])
        add_positive("ev_ebitda", "EV_EBITDA", 0.14, [(10, 90), (15, 78), (22, 63), (30, 48), (45, 35), (10**9, 25)])

        fcf_yield_raw = d.get("FCF_YIELD")
        if fcf_yield_raw is not None:
            fcf_yield = _safe_float_value(fcf_yield_raw, 0.0)
            valuation_inputs_used["fcf_yield_pct"] = fcf_yield
            if fcf_yield >= 8:
                fcf_score = 92
            elif fcf_yield >= 5:
                fcf_score = 80
            elif fcf_yield >= 3:
                fcf_score = 68
            elif fcf_yield >= 1:
                fcf_score = 55
            elif fcf_yield >= 0:
                fcf_score = 42
            else:
                fcf_score = 25
            valuation_parts.append((fcf_score, 0.14))
        else:
            valuation_missing.append("fcf_yield_pct")

        # Small supplemental weights; useful for ETFs and when enterprise multiples are missing.
        add_positive("ps_ratio", "PS_RATIO", 0.04, [(3, 82), (6, 70), (10, 58), (15, 45), (10**9, 32)])
        add_positive("pb_ratio", "PB_RATIO", 0.02, [(2, 75), (5, 65), (10, 52), (20, 42), (10**9, 35)])

    valuation_raw = _weighted_average(valuation_parts) if valuation_applicable else None
    valuation_score = _clamp_score(valuation_raw) if valuation_raw is not None else None
    if not valuation_applicable:
        valuation_status = "NOT_APPLICABLE"
    elif valuation_score is None:
        valuation_status = "MISSING"
    else:
        valuation_status = _score_status(valuation_inputs_used, valuation_missing, weak_when_lt=3)

    # --- Analyst score: EQUITY only. ETFs/indexes/crypto do not receive a fake target-price score. ---
    analyst_applicable = instrument_type == "EQUITY"
    analyst_inputs_used: dict[str, Any] = {}
    analyst_missing: list[str] = []
    analyst_parts: list[tuple[float | None, float]] = []
    target_mean = _safe_float_value(d.get("TARGET_MEAN"), 0.0)
    target_hi = _safe_float_value(d.get("TARGET_HI"), 0.0)
    target_lo = _safe_float_value(d.get("TARGET_LO"), 0.0)
    last = _safe_float_value(d.get("LAST_CLOSE"), 0.0)
    analyst_cnt = int(_safe_float_value(d.get("ANALYST_CNT"), 0.0))
    analyst_rating_text = str(d.get("ANALYST_RATING") or "")
    tgt_upside = (target_mean - last) / last * 100 if target_mean > 0 and last > 0 else None

    if analyst_applicable:
        if tgt_upside is not None:
            analyst_inputs_used["target_mean"] = target_mean
            analyst_inputs_used["target_upside_pct"] = round(tgt_upside, 1)
            target_score = 94 if tgt_upside >= 60 else 84 if tgt_upside >= 35 else 70 if tgt_upside >= 15 else 56 if tgt_upside >= 0 else 38 if tgt_upside >= -20 else 25
            analyst_parts.append((target_score, 0.45))
        else:
            analyst_missing.append("target_mean_or_last_close")
        if analyst_cnt > 0:
            analyst_inputs_used["analyst_count"] = analyst_cnt
            coverage_score = 88 if analyst_cnt >= 30 else 78 if analyst_cnt >= 15 else 65 if analyst_cnt >= 5 else 52
            analyst_parts.append((coverage_score, 0.20))
        else:
            analyst_missing.append("analyst_count")
        consensus_score = _analyst_rating_text_score(analyst_rating_text)
        if consensus_score is not None:
            analyst_inputs_used["analyst_rating"] = analyst_rating_text
            analyst_parts.append((consensus_score, 0.25))
        else:
            analyst_missing.append("analyst_rating")
        if target_hi > 0 and target_lo > 0 and target_mean > 0 and target_hi >= target_lo:
            spread = (target_hi - target_lo) / target_mean
            analyst_inputs_used.update({"target_high": target_hi, "target_low": target_lo, "target_dispersion_pct": round(spread * 100, 1)})
            analyst_parts.append((_clamp_score(85 - spread * 45, 25, 90), 0.10))
        else:
            analyst_missing.append("target_high_low_dispersion")

    analyst_raw = _weighted_average(analyst_parts) if analyst_applicable else None
    analyst_score = _clamp_score(analyst_raw) if analyst_raw is not None else None
    if not analyst_applicable:
        analyst_status = "NOT_APPLICABLE"
    elif analyst_score is None:
        analyst_status = "MISSING"
    else:
        analyst_status = _score_status(analyst_inputs_used, analyst_missing, weak_when_lt=2)

    # --- Risk score: note-derived plus deterministic volatility/drawdown and StockAnalysis balance-sheet ratios. ---
    risk_terms = re.compile(r"debt|债务|capex|资本支出|资本开支|free cash flow|FCF|自由现金流|dilution|稀释|downgrade|下调|融资|利率|yield|收益率|inflation|通胀|concentration|集中度|volatility|波动率|lawsuit|监管|裁员|评级下调|BBB-|junk|垃圾级|liquidity|流动性", re.I)
    risk_hits = 0.0
    risk_evidence_examples: list[str] = []
    risk_note_breakdown = {"BEAR": 0, "MIX": 0, "BULL_IGNORED": 0}
    for n in non_technical_notes:
        text = " ".join([str(getattr(n, "title", "")), str(getattr(n, "fact", "")), str(getattr(n, "logic", ""))])
        tag = str(getattr(n, "tag", "") or "").upper()
        has_risk_term = bool(risk_terms.search(text))
        contribution = 0.0
        if tag == "BEAR":
            contribution = 2.0 + (1.0 if has_risk_term else 0.0)
            risk_note_breakdown["BEAR"] += 1
        elif tag == "MIX" and has_risk_term:
            contribution = 0.5
            risk_note_breakdown["MIX"] += 1
        elif tag == "BULL" and has_risk_term:
            # A bullish item may mention capex/debt/rates as context. Do not punish it
            # solely because a risk keyword appears in an otherwise bullish thesis.
            risk_note_breakdown["BULL_IGNORED"] += 1
        risk_hits += contribution
        if contribution > 0 and len(risk_evidence_examples) < 5:
            risk_evidence_examples.append(str(getattr(n, "title", "")))

    risk_penalty_parts: dict[str, float] = {}
    note_penalty = min(35.0, risk_hits * 2.5)
    risk_penalty_parts["notes"] = note_penalty

    beta = _safe_float_value(d.get("BETA"), 0.0)
    if instrument_type in {"EQUITY", "ETF"} and beta > 0:
        risk_penalty_parts["beta"] = 8.0 if beta >= 2.0 else 5.0 if beta >= 1.5 else 2.5 if beta >= 1.2 else 0.0

    realized_vol = d.get("REALIZED_VOL_20D_PCT")
    if realized_vol is not None:
        rv = _safe_float_value(realized_vol, 0.0)
        # Crypto naturally has higher volatility, so use a higher threshold.
        if instrument_type == "CRYPTO":
            risk_penalty_parts["realized_volatility"] = 12.0 if rv >= 100 else 8.0 if rv >= 70 else 4.0 if rv >= 45 else 0.0
        else:
            risk_penalty_parts["realized_volatility"] = 12.0 if rv >= 60 else 8.0 if rv >= 45 else 4.0 if rv >= 30 else 0.0

    max_dd = d.get("MAX_DRAWDOWN_63D_PCT")
    if max_dd is not None:
        dd = _safe_float_value(max_dd, 0.0)
        risk_penalty_parts["max_drawdown_63d"] = 12.0 if dd >= 30 else 8.0 if dd >= 20 else 4.0 if dd >= 10 else 0.0

    atr_pct_val = d.get("ATR_PCT")
    if atr_pct_val is not None:
        ap = _safe_float_value(atr_pct_val, 0.0)
        risk_penalty_parts["atr_pct"] = 8.0 if ap >= 7 else 5.0 if ap >= 4 else 2.0 if ap >= 2.5 else 0.0

    # Structured balance-sheet risk only applies to equities when available.
    if instrument_type == "EQUITY":
        debt_ebitda = d.get("DEBT_EBITDA")
        if debt_ebitda is not None:
            de = _safe_float_value(debt_ebitda, 0.0)
            risk_penalty_parts["debt_ebitda"] = 12.0 if de >= 5 else 8.0 if de >= 3.5 else 4.0 if de >= 2 else 0.0
        interest_cov = d.get("INTEREST_COVERAGE")
        if interest_cov is not None:
            ic = _safe_float_value(interest_cov, 0.0)
            risk_penalty_parts["interest_coverage"] = 12.0 if ic < 1.5 else 8.0 if ic < 3 else 4.0 if ic < 5 else 0.0
        fcf_yield_for_risk = d.get("FCF_YIELD")
        if fcf_yield_for_risk is not None and _safe_float_value(fcf_yield_for_risk, 0.0) < 0:
            risk_penalty_parts["negative_fcf_yield"] = 8.0

    risk_penalty = min(70.0, sum(risk_penalty_parts.values()))
    risk_score = _clamp_score(100.0 - risk_penalty)
    risk_inputs_used = {
        "risk_hits_from_notes": round(risk_hits, 2),
        "risk_note_breakdown": risk_note_breakdown,
        "bear_notes": counts["BEAR"],
        "risk_penalty_parts": risk_penalty_parts,
        "realized_vol_20d_pct": d.get("REALIZED_VOL_20D_PCT"),
        "max_drawdown_63d_pct": d.get("MAX_DRAWDOWN_63D_PCT"),
        "atr_pct": d.get("ATR_PCT"),
        "beta": beta if instrument_type in {"EQUITY", "ETF"} else None,
        "debt_ebitda": d.get("DEBT_EBITDA") if instrument_type == "EQUITY" else None,
        "interest_coverage": d.get("INTEREST_COVERAGE") if instrument_type == "EQUITY" else None,
        "risk_evidence_examples": risk_evidence_examples,
    }

    # Nominal weights by instrument type. Missing/applicable scores are excluded and reweighted.
    nominal_profiles = {
        "EQUITY": {"technical_score": 0.25, "news_score": 0.25, "valuation_score": 0.20, "analyst_score": 0.10, "risk_score": 0.20},
        "ETF": {"technical_score": 0.30, "news_score": 0.25, "valuation_score": 0.20, "risk_score": 0.25},
        "INDEX": {"technical_score": 0.40, "news_score": 0.30, "risk_score": 0.30},
        "CRYPTO": {"technical_score": 0.40, "news_score": 0.30, "risk_score": 0.30},
        "OTHER": {"technical_score": 0.40, "news_score": 0.30, "risk_score": 0.30},
    }
    nominal_weights = nominal_profiles.get(instrument_type, nominal_profiles["OTHER"])
    scores: dict[str, float | None] = {
        "technical_score": technical_score,
        "news_score": news_score,
        "valuation_score": valuation_score,
        "analyst_score": analyst_score,
        "risk_score": risk_score,
    }
    available_weight = sum(weight for key, weight in nominal_weights.items() if scores.get(key) is not None)
    effective_weights = {
        key: round(weight / available_weight, 4)
        for key, weight in nominal_weights.items()
        if scores.get(key) is not None and available_weight > 0
    }
    if not effective_weights:
        final_score = 50.0
    else:
        final_score = _clamp_score(sum(float(scores[key]) * weight for key, weight in effective_weights.items()))
    label, cls = _rating_label(final_score, instrument_type)

    score_status = {
        "technical_score": "FULL" if technical_score is not None and not d.get("technical_unavailable_components") else "PARTIAL" if technical_score is not None else "MISSING",
        "news_score": "PARTIAL" if news_score is not None else "MISSING",
        "valuation_score": valuation_status,
        "analyst_score": analyst_status,
        "risk_score": "PARTIAL",
    }

    audit = {
        "ticker": ctx.ticker,
        "report_date": ctx.report_date,
        "method": "v5.8_instrument_aware_score_input_audit",
        "instrument_type": instrument_type,
        "scoring_profile": scoring_profile,
        "nominal_weights": nominal_weights,
        "effective_weights": effective_weights,
        "applicable_scores": list(nominal_weights.keys()),
        "not_applicable_scores": [key for key, status in score_status.items() if status == "NOT_APPLICABLE"],
        "fundamental_sources": sources,
        "stockanalysis": {"enabled": d.get("STOCKANALYSIS_ENABLED"), "data": d.get("STOCKANALYSIS_DATA") or {}, "error": d.get("STOCKANALYSIS_ERROR") or ""},
        "technical_score": {"score": round(technical_score, 1) if technical_score is not None else None, "status": score_status["technical_score"], "inputs_used": technical_used, "inputs_missing": d.get("technical_unavailable_components") or [], "note": "Deterministic OHLCV score; unavailable volume/chip components are excluded and weights are renormalized."},
        "news_score": {"score": round(news_score, 1) if news_score is not None else None, "status": score_status["news_score"], "inputs_used": {"note_counts": counts, "ab_or_tech_evidence_notes": ab_notes, "total_notes": len(notes), "quality_bonus": round(quality_bonus, 1)}, "inputs_missing": ["independent_raw_news_sentiment_classifier"], "note": "Derived from evidence-bound final notes."},
        "valuation_score": {"score": round(valuation_score, 1) if valuation_score is not None else None, "status": valuation_status, "inputs_used": valuation_inputs_used, "inputs_missing": valuation_missing if valuation_applicable else [], "source_preference": "StockAnalysis first; no peer comparison or historical valuation percentile in v5.8.", "note": "Applicable to equities and ETFs only; target price is excluded."},
        "analyst_score": {"score": round(analyst_score, 1) if analyst_score is not None else None, "status": analyst_status, "inputs_used": analyst_inputs_used, "inputs_missing": analyst_missing if analyst_applicable else [], "source_preference": "StockAnalysis consensus/price target first; yfinance coverage and target range fallback."},
        "risk_score": {"score": round(risk_score, 1), "status": "PARTIAL", "inputs_used": risk_inputs_used, "inputs_missing": [], "note": "Combines evidence-bound BEAR/MIX risk notes with realized volatility, drawdown, ATR and available balance-sheet ratios. BULL notes are not penalized merely for containing risk keywords."},
    }

    payload = {
        "final_score": round(final_score, 1),
        "rating_text": label,
        "rating_class": cls,
        "method": "v5.8_instrument_aware_dynamic_weights",
        "instrument_type": instrument_type,
        "scoring_profile": scoring_profile,
        "nominal_weights": nominal_weights,
        "effective_weights": effective_weights,
        "subscores": {key: (round(value, 1) if value is not None else None) for key, value in scores.items()},
        "score_status": score_status,
        "inputs": {
            "note_counts": counts,
            "ab_or_tech_evidence_notes": ab_notes,
            "non_technical_notes": non_tech_count,
            "target_upside_pct": round(tgt_upside, 1) if tgt_upside is not None else None,
            "valuation_inputs": valuation_inputs_used,
            "analyst_inputs": analyst_inputs_used,
            "risk_penalty_parts": risk_penalty_parts,
        },
        "score_input_audit_file": str(ctx.run_dir / f"{ctx.ticker}_score_input_audit.json"),
        "explanation": "按标的类型选择适用评分项；指数与ETF均保留完整OHLCV、成交量和筹码峰技术分析；NOT_APPLICABLE 或缺失项不填充中性50分，而是从最终加权中排除并重新归一化有效权重。",
    }
    try:
        if ctx.data_file.exists():
            current = json.loads(ctx.data_file.read_text(encoding="utf-8"))
            current["final_rating"] = payload
            current["score_input_audit"] = audit
            ctx.data_file.write_text(json_dumps(current), encoding="utf-8")
    except Exception:
        pass
    try:
        (ctx.run_dir / f"{ctx.ticker}_final_rating.json").write_text(json_dumps(payload), encoding="utf-8")
        (ctx.run_dir / f"{ctx.ticker}_score_input_audit.json").write_text(json_dumps(audit), encoding="utf-8")
    except Exception:
        pass
    return payload



class SaveNewsNotesTool(BaseTool):
    name = "save_news_notes"
    description = (
        "将结构化消息面分析保存为 build_report.py 可读取的 notes.txt。"
        "输入必须是 items 数组，每条包含 tag/title/fact/logic/investment_meaning/source/source_date/url。"
        "本工具会严格校验数量、BULL/BEAR/MIX 比例、事实、逻辑和投资含义。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string", "enum": ["BULL", "BEAR", "MIX"]},
                        "title": {"type": "string"},
                        "fact": {"type": "string"},
                        "logic": {"type": "string"},
                        "investment_meaning": {"type": "string"},
                        "source": {"type": "string"},
                        "source_date": {"type": "string"},
                        "url": {"type": "string"},
                        "evidence_id": {"type": "string", "description": "必须绑定 evidence.json/articles.json 中的 evidence_id；技术面条目用 TECH-001/TECH-002/TECH-003。"},
                    },
                    "required": ["tag", "title", "fact", "logic", "investment_meaning", "evidence_id"],
                },
            }
        },
        "required": ["items"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        try:
            p = parse_tool_params(params)
            notes = parse_notes_payload(p)

            # V5.8: auto-append deterministic technical notes unless explicitly disabled.
            # This prevents the model from accidentally binding RSI/MA/chip-profile facts to a news evidence id.
            if os.environ.get("AUTO_APPEND_TECHNICAL_NOTES", "true").strip().lower() not in {"0", "false", "no"}:
                existing_tech_ids = {str(getattr(n, "evidence_id", "")).upper() for n in notes if getattr(n, "is_technical", False)}
                try:
                    tech_payload = json.loads(GenerateTechnicalNoteItemsTool().call({}))
                    tech_items = tech_payload.get("items", []) if isinstance(tech_payload, dict) else []
                    if isinstance(tech_items, list):
                        for tech_note in parse_notes_payload({"items": tech_items}):
                            eid = str(getattr(tech_note, "evidence_id", "")).upper()
                            if eid and eid not in existing_tech_ids:
                                notes.append(tech_note)
                                existing_tech_ids.add(eid)
                except Exception:
                    pass

            # V5 evidence gate: every non-technical note must bind to a local, verifiable
            # evidence/article record. DashScope candidates are intentionally excluded.
            records = _collect_verifiable_records(ctx)
            binding_errors: list[str] = []
            unknown_bound = 0
            for idx, note in enumerate(notes, start=1):
                bound_ok, record, reason = _resolve_note_to_evidence(note, records)
                if not bound_ok:
                    binding_errors.append(f"第 {idx} 条【{note.title}】证据绑定失败：{reason}")
                    continue
                if record:
                    if os.environ.get("DISALLOW_USD_CHINESE_YI", "true").strip().lower() not in {"0", "false", "no"} and _note_has_forbidden_usd_chinese_yi(note):
                        binding_errors.append(
                            f"第 {idx} 条【{note.title}】包含易误解的美元中文金额格式（如 $17.2亿）。请改为 $17.2B / 172亿美元，或直接保留 B/M 单位。"
                        )
                        continue
                    eid = str(record.get("evidence_id") or note.evidence_id or "").strip()
                    note.evidence_id = note.evidence_id or eid
                    note.evidence_url = str(record.get("url") or record.get("final_url") or note.url or "")
                    note.evidence_title = str(record.get("title") or note.evidence_title or "")
                    note.evidence_method = str(record.get("evidence_method") or record.get("method") or "")
                    note.source_domain = str(record.get("source_domain") or _source_domain(note.evidence_url) or "")
                    grade = str(record.get("evidence_grade") or _evidence_grade(record))
                    note.evidence_grade = grade
                    note.evidence_origin = str(record.get("evidence_origin") or _evidence_origin(record))
                    note.evidence_allowed_uses = str(record.get("evidence_allowed_uses") or _evidence_allowed_uses(grade))
                    note.evidence_support_excerpt = str(record.get("support_text_excerpt") or _support_excerpt(record))
                    if os.environ.get("STRICT_NUMERIC_SUPPORT", "false").strip().lower() in {"1", "true", "yes"} and not getattr(note, "is_technical", False) and _note_contains_hard_fact(note):
                        ratio, missing_nums = _numeric_support_ratio(note, record)
                        min_ratio = float(os.environ.get("NUMERIC_SUPPORT_MIN_RATIO", "0.5"))
                        if ratio < min_ratio:
                            binding_errors.append(
                                f"第 {idx} 条【{note.title}】核心数字缺少足够证据支撑；支持比例 {ratio:.0%} < {min_ratio:.0%}，缺失数字示例：{missing_nums[:6]}。"
                            )
                            continue
                    if os.environ.get("STRICT_EVIDENCE_GRADING", "true").strip().lower() not in {"0", "false", "no"}:
                        if grade == "D" and _note_contains_hard_fact(note):
                            binding_errors.append(
                                f"第 {idx} 条【{note.title}】使用 D 级来源支撑硬财务/评级/估值结论，不允许；请改用 IR/SEC/Reuters/CNBC/Yahoo/Morningstar 等 A/B 级证据。"
                            )
                            continue
                        if os.environ.get("HARD_FACT_REQUIRE_AB", "false").strip().lower() in {"1", "true", "yes"}:
                            if grade not in {"A", "B"} and _note_contains_hard_fact(note):
                                binding_errors.append(
                                    f"第 {idx} 条【{note.title}】硬事实需要 A/B 级来源，当前为 {grade} 级。"
                                )
                                continue
                    if not note.url and note.evidence_url:
                        note.url = note.evidence_url
                    if not note.source:
                        note.source = str(record.get("source") or record.get("source_domain") or note.source_domain or "")
                    if not note.source_date or note.source_date.lower() in {"unknown", "none", "null"}:
                        record_date = str(record.get("source_date") or record.get("published_date") or "")
                        if record_date:
                            note.source_date = record_date
                    if note.source_date.lower() in {"", "unknown", "none", "null"}:
                        unknown_bound += 1

            max_unknown_bound = int(os.environ.get("NOTES_MAX_UNKNOWN_SOURCE_DATE", "3"))
            if max_unknown_bound >= 0 and unknown_bound > max_unknown_bound:
                binding_errors.append(f"绑定后的 source_date=unknown 条目过多：{unknown_bound} 条，最多允许 {max_unknown_bound} 条。")

            ok, errors = validate_notes(notes, min_items=ctx.min_notes)
            errors.extend(binding_errors)
            if not ok or binding_errors:
                return json_dumps({
                    "ok": False,
                    "errors": errors,
                    "hint": "V5.1 要求每条非技术面 note 必须绑定 evidence_id（来自 evidence.json/articles.json/dashscope_sources.json）。DashScope candidate 只能用于启发搜索；只有 DS-xxx source objects 可直接引用。",
                    "allowed_evidence_ids": sorted(records.keys())[:80],
                    "counts": {tag: sum(1 for n in notes if n.tag == tag) for tag in ["BULL", "BEAR", "MIX"]},
                })

            text = render_notes_text(notes)
            ctx.notes_file.write_text(text, encoding="utf-8")
            final_rating_payload = _compute_final_rating_payload(ctx, notes)
            final_notes_payload = {
                "ticker": ctx.ticker,
                "report_date": ctx.report_date,
                "count": len(notes),
                "counts": {tag: sum(1 for n in notes if n.tag == tag) for tag in ["BULL", "BEAR", "MIX"]},
                "items": notes_to_jsonable(notes),
                "final_rating": final_rating_payload,
                "evidence_files": {
                    "evidence_file": str(ctx.evidence_file),
                    "articles_file": str(ctx.articles_file),
                    "candidates_file": str(ctx.candidates_file),
                    "dashscope_sources_file": str(ctx.dashscope_sources_file),
                },
            }
            ctx.final_notes_json_file.write_text(json_dumps(final_notes_payload), encoding="utf-8")
            return json_dumps({
                "ok": True,
                "notes_file": str(ctx.notes_file),
                "final_notes_json_file": str(ctx.final_notes_json_file),
                "count": len(notes),
                "counts": {tag: sum(1 for n in notes if n.tag == tag) for tag in ["BULL", "BEAR", "MIX"]},
                "preview": text[:1200],
                "structured_items": notes_to_jsonable(notes),
                "final_rating": final_rating_payload,
            })
        except Exception as exc:
            return json_dumps({"ok": False, "errors": [str(exc)]})


class GenerateChartTool(BaseTool):
    name = "generate_technical_chart"
    description = "执行 scripts/gen_chart.py 生成包含 K线、均线、布林带、成交量、MACD、RSI、KDJ 的 Plotly HTML 图表片段。"
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "months": {"type": "integer", "description": "K线图月份数，默认使用 CLI 传入的 months"},
        },
        "required": ["ticker"],
    }

    def call(self, params: str | dict, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        ticker = str(p.get("ticker") or ctx.ticker).strip().upper()
        months = int(p.get("months") or ctx.months)
        chart = ensure_within_dir(ctx.chart_file, ctx.run_dir)
        result = run_python_script(
            ctx.paths.scripts_dir / "gen_chart.py",
            [ticker, str(chart), "--months", str(months)],
            cwd=ctx.run_dir,
            timeout=240,
        )
        return json_dumps({"ok": True, "chart_file": str(chart), "stdout_tail": result["stdout"], "size_bytes": chart.stat().st_size})


class BuildHtmlReportTool(BaseTool):
    name = "build_html_report"
    description = "执行 scripts/build_report.py，把 data.json、chart.html 和 notes.txt 拼装成最终暗色交互式 HTML 日报。"
    parameters = {
        "type": "object",
        "properties": {
            "use_notes": {"type": "boolean", "description": "是否传入已生成的 notes 文件，默认 true"}
        },
        "required": [],
    }

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_context()
        p = parse_tool_params(params)
        use_notes = bool(p.get("use_notes", True))
        if not ctx.data_file.exists():
            return json_dumps({"ok": False, "errors": ["缺少 data_file，请先调用 fetch_technical_data。"]})
        if not ctx.chart_file.exists():
            return json_dumps({"ok": False, "errors": ["缺少 chart_file，请先调用 generate_technical_chart。"]})
        args = [str(ctx.data_file), str(ctx.chart_file), str(ctx.final_output_html), "--date", ctx.report_date]
        if use_notes:
            if not ctx.notes_file.exists():
                return json_dumps({"ok": False, "errors": ["缺少 notes_file，请先调用 save_news_notes，或 use_notes=false。"]})
            args.extend(["--notes", str(ctx.notes_file)])
        result = run_python_script(ctx.paths.scripts_dir / "build_report.py", args, cwd=ctx.run_dir, timeout=180)
        return json_dumps({
            "ok": True,
            "output_html": str(ctx.final_output_html),
            "size_bytes": ctx.final_output_html.stat().st_size if ctx.final_output_html.exists() else 0,
            "stdout_tail": result["stdout"],
        })


class InspectRunStateTool(BaseTool):
    name = "inspect_report_run_state"
    description = "检查当前日报生成流程已经产生了哪些文件，帮助 agent 判断下一步该调用哪个工具。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def call(self, params: str | dict | None = None, **kwargs) -> str:
        ctx = get_context()
        files = {}
        for label, path in {
            "data_file": ctx.data_file,
            "chart_file": ctx.chart_file,
            "notes_file": ctx.notes_file,
            "evidence_file": ctx.evidence_file,
            "articles_file": ctx.articles_file,
            "final_notes_json_file": ctx.final_notes_json_file,
            "final_rating_file": ctx.run_dir / f"{ctx.ticker}_final_rating.json",
            "candidates_file": ctx.candidates_file,
            "searxng_raw_results_file": ctx.raw_results_file,
            "searxng_reranked_evidence_file": ctx.reranked_evidence_file,
            "serper_raw_results_file": ctx.serper_raw_results_file,
            "serper_reranked_evidence_file": ctx.serper_reranked_evidence_file,
            "combined_reranked_evidence_file": ctx.combined_reranked_evidence_file,
            "search_quality_report_file": ctx.search_quality_report_file,
            "audit_file": ctx.audit_file,
            "output_html": ctx.final_output_html,
        }.items():
            files[label] = {
                "path": str(path),
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else 0,
            }
        return json_dumps({
            "ticker": ctx.ticker,
            "market_type": infer_market_type(ctx.ticker),
            "recommended_search_languages": infer_search_languages(ctx.ticker),
            "run_dir": str(ctx.run_dir),
            "files": files,
        })


def build_custom_tools() -> list[BaseTool]:
    return [
        ReadSkillTool(),
        ReadTickerReferenceTool(),
        ValidateTickerTool(),
        FetchTechnicalDataTool(),
        SearXNGSearchTool(),
        SearXNGMarketResearchTool(),
        SerperSearchTool(),
        SerperMarketResearchTool(),
        CombinedMarketResearchTool(),
        PriorityMarketResearchTool(),
        SearchQualityReportTool(),
        FetchArticleTextTool(),
        GenerateTechnicalNoteItemsTool(),
        DashScopeMarketResearchTool(),
        SaveEvidenceTool(),
        SaveNewsNotesTool(),
        GenerateChartTool(),
        BuildHtmlReportTool(),
        InspectRunStateTool(),
    ]
