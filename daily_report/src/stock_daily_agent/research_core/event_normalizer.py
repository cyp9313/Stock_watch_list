# -*- coding: utf-8 -*-
"""Event Normalizer（修改计划第六轮第 17 节）。

把分散的搜索结果归一化为结构化事件，提取 event_type / event_date，
为后续 Event Clustering 提供基础。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


# event_need → event_type 标准化映射
EVENT_NEED_TO_TYPE: dict[str, str] = {
    "latest_official_filing": "official_filing",
    "earnings_date": "earnings_date",
    "earnings_results": "earnings_results",
    "guidance": "guidance",
    "credit_and_financing": "credit_event",
    "capital_raise": "capital_raise",
    "analyst_revision": "analyst_revision",
    "regulatory": "regulatory_event",
    "litigation": "litigation",
    "product_event": "product_event",
    "major_contract": "major_contract",
    "management_change": "management_change",
    "governance": "governance",
    "merger_acquisition": "merger_acquisition",
    "index_rebalance": "index_rebalance",
    "fund_flow": "fund_flow",
    "aum_change": "aum_change",
    "premium_discount": "premium_discount",
    "theme_supply": "theme_supply",
    "theme_policy": "theme_policy",
    "commodity_driver": "commodity_driver",
    "crypto_regulation": "crypto_regulation",
    "trading_volume": "trading_volume",
    "security_incident": "security_incident",
    "macro_driver": "macro_driver",
}


# 日期提取正则
_DATE_PATTERNS = [
    (r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", "%Y-%m-%d"),
    (r"\b(20\d{2})/(\d{1,2})/(\d{1,2})\b", "%Y/%m/%d"),
    (r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s+(20\d{2})\b", "%b %d, %Y"),
    (r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(20\d{2})\b", "%d %b %Y"),
    (r"\bQ([1-4])\s+(20\d{2})\b", None),  # Q1 2026 → 季度
]


def normalize_event_type(event_need: str | None, title: str = "", summary: str = "") -> str:
    """把 event_need 标准化为 event_type。

    优先使用 event_need 映射；若缺失则从 title/summary 启发式推断。
    """
    if event_need and event_need in EVENT_NEED_TO_TYPE:
        return EVENT_NEED_TO_TYPE[event_need]
    text = (title + " " + summary).lower()
    if re.search(r"\b(earnings|results|revenue)\b", text):
        return "earnings_results"
    if re.search(r"\b(downgrad\w*|upgrade\w*|target price)\b", text):
        return "analyst_revision"
    if re.search(r"\b(lawsuit|litigation|probe|investigation)\b", text):
        return "litigation"
    if re.search(r"\b(recall|safety|breach|cyber)\b", text):
        return "product_event"
    if re.search(r"\b(merger|acquisition|buyout)\b", text):
        return "merger_acquisition"
    if re.search(r"\b(regulatory|fda|sec|approval)\b", text):
        return "regulatory_event"
    if re.search(r"\b(dividend|buyback)\b", text):
        return "capital_raise"
    return "general_event"


def extract_event_date(published_date: str | None, title: str = "", summary: str = "") -> str | None:
    """从 published_date / title / summary 中提取事件日期（ISO 格式 YYYY-MM-DD）。"""
    # 优先用 published_date
    pd = str(published_date or "").strip()
    if pd and len(pd) >= 10:
        try:
            return date.fromisoformat(pd[:10]).isoformat()
        except (ValueError, TypeError):
            pass

    text = (title + " " + summary)
    for pat, fmt in _DATE_PATTERNS:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        if fmt is None:
            # Q1 2026 → 返回该季度起始日
            q = int(m.group(1))
            year = int(m.group(2))
            month = (q - 1) * 3 + 1
            try:
                return date(year, month, 1).isoformat()
            except ValueError:
                continue
        try:
            raw = m.group(0)
            # 标准化月份缩写
            return datetime.strptime(raw, fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    return None


def normalize_event(
    result: dict[str, Any],
    *,
    ticker: str | None,
    event_need: str | None,
) -> dict[str, Any]:
    """把搜索结果归一化为事件结构。"""
    title = str(result.get("title") or "")
    summary = str(result.get("summary") or "")
    event_type = normalize_event_type(event_need, title, summary)
    event_date = extract_event_date(result.get("published_date"), title, summary)
    return {
        "event_type": event_type,
        "event_date": event_date,
        "event_need": event_need,
        "ticker": ticker,
        "title": title,
        "summary": summary,
        "url": result.get("url"),
        "source_name": result.get("source_name") or result.get("source"),
        "published_date": result.get("published_date"),
    }
