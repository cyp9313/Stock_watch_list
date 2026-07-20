# -*- coding: utf-8 -*-
"""Research Query Compiler（修改计划第六轮第 13 节）。

Planner 输出不能直接原样发送给 Serper / SearXNG。本模块负责：

- 清理引号和非法操作符；
- 添加时间范围（``after:YYYY-MM-DD``）；
- 添加语言提示（hl/gl 仅作为元信息，不写入 query 文本）；
- 添加 official domain（``site:`` 仅在 official lane 使用，且受
  ``PORTFOLIO_RESEARCH_PLANNER_ALLOW_SITE_OPERATOR`` 控制）；
- 控制 query 长度；
- 防止重复（同 ticker / 同 event_need / 相同归一化 query 文本）；
- 输出 Serper / SearXNG 可执行结构。
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from .research_plan_schema import ALLOWED_LOOKBACK_DAYS, is_valid_lookback_days


# 是否允许在 official lane 使用 site: 操作符（默认允许，因为 preferred_domains
# 是 Planner 显式声明的官方域名；如果担心被搜索引擎误判可关闭）
_ALLOW_SITE_OPERATOR = os.environ.get(
    "PORTFOLIO_RESEARCH_PLANNER_ALLOW_SITE_OPERATOR", "true",
).strip().lower() in {"1", "true", "yes"}

# query 文本最大长度
_MAX_QUERY_LEN = 200


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _clean_query_text(text: str) -> str:
    """清理 query 文本：去除多余空白、引号、非法操作符前缀。"""
    s = (text or "").strip()
    # 去除首尾引号
    s = s.strip("\"'“”‘’")
    # 折叠空白
    s = re.sub(r"\s+", " ", s)
    # 去除 query 内部的多余 site:/OR 等操作符（统一由 compiler 控制）
    s = re.sub(r"\b(?:site|inurl|intitle|filetype):[^\s]+", "", s, flags=re.I)
    s = re.sub(r"\bOR\b", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _clean_domain(value: str) -> str:
    """Normalize a preferred domain for a ``site:`` query.

    Planner/metadata may provide either ``example.com`` or a full URL/path.
    Only the hostname is valid after the ``site:`` operator.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    domain = (parsed.netloc or parsed.path.split("/", 1)[0]).strip().removeprefix("www.")
    return re.sub(r"[^a-z0-9.-]", "", domain)


def _normalize_query(text: str) -> str:
    """归一化用于去重比较。"""
    s = _clean_query_text(text).lower()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _date_filter(lookback_days: int) -> str:
    """生成 Serper/Google 的 after:YYYY-MM-DD 时间过滤。"""
    if lookback_days <= 0:
        return ""
    start = date.today() - timedelta(days=int(lookback_days))
    return f"after:{start.isoformat()}"


def compile_research_queries(
    plan: dict[str, Any],
    *,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
    benchmark: str = "^GSPC",
) -> list[dict[str, Any]]:
    """把校验后的 Research Plan 编译为 Serper / SearXNG 可执行的 query 列表。

    返回结构（每条）：
        {
            "query": "<最终 query 文本，含 after: 过滤>",
            "raw_query": "<清理后的 query 文本，不含时间过滤>",
            "language": "en" | "zh-CN",
            "lookback_days": 30,
            "lane": "official_and_news" | "news" | "theme" | "macro",
            "ticker": "ORCL" | None,
            "event_need": "credit_and_financing",
            "question_id": "ORCL_Q1",
            "preferred_domains": ["investor.oracle.com", ...],
            "required_entities": ["Oracle"],
            "exclude_terms": ["Playtech"],
            "use_news_vertical": True | False,
            "scope": "ticker" | "macro",
        }
    """
    instrument_metadata = instrument_metadata or {}
    if not isinstance(plan, dict):
        return []

    compiled: list[dict[str, Any]] = []
    seen_normalized: set[str] = set()

    for ticker_entry in plan.get("tickers") or []:
        if not isinstance(ticker_entry, dict):
            continue
        ticker = str(ticker_entry.get("ticker") or "").strip().upper()
        primary_language = str(ticker_entry.get("primary_language") or "en").strip()
        for q in ticker_entry.get("research_questions") or []:
            if not isinstance(q, dict):
                continue
            event_need = str(q.get("event_need") or "general")
            lane = str(q.get("lane") or "news")
            lookback = q.get("lookback_days")
            if not is_valid_lookback_days(lookback):
                lookback = 30
            lookback_int = int(lookback)
            question_id = str(q.get("question_id") or "")
            preferred_domains = list(q.get("preferred_domains") or [])
            required_entities = list(q.get("required_entities") or [])
            exclude_terms = list(q.get("exclude_terms") or [])
            use_news_vertical = lane in {"news", "official_and_news"}

            queries = q.get("queries") or []
            for raw_q in queries:
                cleaned = _clean_query_text(str(raw_q))
                if not cleaned:
                    continue
                if len(cleaned) > _MAX_QUERY_LEN:
                    cleaned = cleaned[:_MAX_QUERY_LEN].strip()

                normalized = _normalize_query(cleaned)
                dedup_key = f"{ticker}|{event_need}|{normalized}"
                if dedup_key in seen_normalized:
                    continue
                seen_normalized.add(dedup_key)

                # 时间过滤
                date_filter = _date_filter(lookback_int)
                final_query = f"{cleaned} {date_filter}".strip() if date_filter else cleaned

                compiled.append({
                    "query": final_query,
                    "raw_query": cleaned,
                    "language": primary_language,
                    "lookback_days": lookback_int,
                    "lane": lane,
                    "ticker": ticker,
                    "event_need": event_need,
                    "question_id": question_id,
                    "preferred_domains": list(preferred_domains),
                    "required_entities": list(required_entities),
                    "exclude_terms": list(exclude_terms),
                    "use_news_vertical": use_news_vertical,
                    "scope": "ticker",
                })

    # macro_questions
    for m in plan.get("macro_questions") or []:
        if not isinstance(m, dict):
            continue
        event_need = str(m.get("event_need") or "macro_driver")
        lane = str(m.get("lane") or "macro")
        lookback = m.get("lookback_days")
        if not is_valid_lookback_days(lookback):
            lookback = 45
        lookback_int = int(lookback)
        question_id = str(m.get("question_id") or "")
        preferred_domains = list(m.get("preferred_domains") or [])
        use_news_vertical = lane == "news"

        for raw_q in (m.get("queries") or []):
            cleaned = _clean_query_text(str(raw_q))
            if not cleaned:
                continue
            if len(cleaned) > _MAX_QUERY_LEN:
                cleaned = cleaned[:_MAX_QUERY_LEN].strip()
            normalized = _normalize_query(cleaned)
            dedup_key = f"MACRO|{event_need}|{normalized}"
            if dedup_key in seen_normalized:
                continue
            seen_normalized.add(dedup_key)
            date_filter = _date_filter(lookback_int)
            final_query = f"{cleaned} {date_filter}".strip() if date_filter else cleaned
            compiled.append({
                "query": final_query,
                "raw_query": cleaned,
                "language": "en",
                "lookback_days": lookback_int,
                "lane": lane,
                "ticker": None,
                "event_need": event_need,
                "question_id": question_id,
                "preferred_domains": list(preferred_domains),
                "required_entities": [],
                "exclude_terms": [],
                "use_news_vertical": use_news_vertical,
                "scope": "macro",
            })

    return compiled


def expand_official_lane_queries(
    compiled: list[dict[str, Any]],
    *,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """对 official_and_news lane 的 query，基于 preferred_domains 展开 site: 查询。

    返回新增的 official lane query 列表（原 query 不变）。每个 preferred_domain
    生成一条 ``site:<domain> <entity> <event keywords>`` 形式的查询。
    """
    instrument_metadata = instrument_metadata or {}
    if not _ALLOW_SITE_OPERATOR:
        return []
    extra: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in compiled:
        if entry.get("lane") != "official_and_news":
            continue
        ticker = entry.get("ticker")
        domains = entry.get("preferred_domains") or []
        if not domains:
            continue
        entities = entry.get("required_entities") or ([ticker] if ticker else [])
        if not entities:
            continue
        # 从 raw_query 提取事件关键词（去掉公司名后剩余部分）
        raw = str(entry.get("raw_query") or "")
        for entity in entities[:1]:
            for domain in domains[:3]:
                clean_domain = _clean_domain(str(domain))
                if not clean_domain:
                    continue
                # 构造 site: 查询：site:domain entity + 关键事件词
                site_query = f"site:{clean_domain} {entity}"
                # 附带事件关键词（限制长度，避免过长）
                keywords = _clean_query_text(raw.replace(str(entity), "")).strip()
                if keywords and len(keywords) < 80:
                    site_query = f"{site_query} {keywords}"
                # Do not call ``_clean_query_text`` on the complete string here:
                # that helper intentionally strips user-supplied operators.  In
                # this function the site operator is compiler-owned and trusted.
                site_query = re.sub(r"\s+", " ", site_query).strip()
                if not site_query:
                    continue
                norm = _normalize_query(site_query)
                key = f"OFFICIAL|{ticker or 'MACRO'}|{norm}"
                if key in seen:
                    continue
                seen.add(key)
                date_filter = _date_filter(int(entry.get("lookback_days") or 30))
                final = f"{site_query} {date_filter}".strip() if date_filter else site_query
                extra.append({
                    "query": final,
                    "raw_query": site_query,
                    "language": entry.get("language") or "en",
                    "lookback_days": int(entry.get("lookback_days") or 30),
                    "lane": "official",
                    "ticker": ticker,
                    "event_need": entry.get("event_need"),
                    "question_id": entry.get("question_id"),
                    "preferred_domains": [clean_domain],
                    "required_entities": list(entities),
                    "exclude_terms": list(entry.get("exclude_terms") or []),
                    "use_news_vertical": False,
                    "scope": "ticker" if ticker else "macro",
                })
    return extra
