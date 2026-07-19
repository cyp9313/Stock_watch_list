# -*- coding: utf-8 -*-
"""Chronology Validator（第七轮修改计划 §15）。

检查 Evidence 事件时间是否与已知财报日历一致：
- 如果内容声称 "reported Q2 results" 但 event_date < official_earnings_date → chronology_conflict
- Forecast / Preview 不能被误当 Actual
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any


# 财报结果关键词（表明事件已被报告，而非 forecast）
_EARNINGS_REPORTED_PATTERNS = [
    re.compile(r"\b(reported|posted|announced|released?|delivered)\b.*\b(results?|earnings?|numbers?|profit|loss)\b", re.I),
    re.compile(r"\b(beat|missed|met|exceeded)\b.*\b(estimate|expectation|consensus|forecast)\b", re.I),
    re.compile(r"\b(q[1-4]|fy\d{2})\b.*\b(results?|earnings?)\b", re.I),
    re.compile(r"\b(revenue|eps|earnings)\b.*\b(grew|rose|increased|fell|dropped|declined)\b.*\b\d+%", re.I),
    re.compile(r"\u53d1\u5e03.*\u8d22\u62a5|\u4e1a\u7ee9.*\u62a5\u544a|\u8d22\u62a5.*\u516c\u5e03", re.I),  # 发布财报/业绩报告
]

# Forecast/Preview 关键词（尚未发生，不能当作 actual）
_FORECAST_PATTERNS = [
    re.compile(r"\b(forecast|estimate|predict|expect|anticipate|project|guidance|outlook)\b", re.I),
    re.compile(r"\b(ahead of|before|upcoming|scheduled|expected to)\b.*\b(earnings|results?|report)\b", re.I),
    re.compile(r"\b(preview|pre-earnings|previewing)\b", re.I),
    re.compile(r"\u9884\u6d4b|\u9884\u671f|\u5c55\u671b|\u5c06\u4e8e|\u9884\u8ba1", re.I),
]


def check_earnings_chronology(
    evidence_item: dict[str, Any],
    instrument_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """检查单条证据的时间线一致性。

    返回 {"status": "ok"|"chronology_conflict"|"likely_forecast", "reason": str}
    """
    if instrument_meta is None:
        instrument_meta = {}

    event_hint = str(evidence_item.get("event_hint") or evidence_item.get("event_need") or "")
    if "earnings" not in event_hint.lower():
        return {"status": "ok", "reason": ""}

    title = str(evidence_item.get("title") or "")
    snippet = str(evidence_item.get("summary") or evidence_item.get("raw_snippet") or "")
    text = f"{title} {snippet}"

    # 检查是否是 forecast/preview
    is_forecast = any(p.search(text) for p in _FORECAST_PATTERNS)
    if is_forecast:
        has_report_claim = any(p.search(text) for p in _EARNINGS_REPORTED_PATTERNS)
        if has_report_claim:
            # 同时包含 forecast 和 reported 词汇 → 可能是混合或串线
            return {"status": "chronology_conflict", "reason": "文本同时包含 forecast 和 reported 词汇，可能串线或日期误识别"}
        return {"status": "likely_forecast", "reason": "内容为 earnings forecast/preview，不得当作实际财报结果"}

    # 检查是否声称已报告
    is_reported = any(p.search(text) for p in _EARNINGS_REPORTED_PATTERNS)
    if not is_reported:
        return {"status": "ok", "reason": ""}

    # 检查事件日期 vs 官方财报日期
    event_date_str = str(evidence_item.get("published_date") or evidence_item.get("raw_published_date") or "")
    if not event_date_str:
        return {"status": "ok", "reason": ""}

    try:
        event_date = date.fromisoformat(str(event_date_str)[:10])
    except (ValueError, TypeError):
        return {"status": "ok", "reason": ""}

    # 从 metadata 获取已知财报日期
    known_events = instrument_meta.get("known_upcoming_events") or []
    for ke in known_events:
        if not isinstance(ke, dict):
            continue
        ke_date_str = str(ke.get("date") or "")
        ke_type = str(ke.get("type") or ke.get("event_type") or "").lower()
        if "earnings" not in ke_type:
            continue
        try:
            ke_date = date.fromisoformat(ke_date_str[:10])
        except (ValueError, TypeError):
            continue

        if event_date < ke_date:
            return {
                "status": "chronology_conflict",
                "reason": (
                    f"事件日期 {event_date_str} 早于官方财报日期 {ke_date_str}，"
                    f"但内容声称 'reported results'。可能为 forecast 被误当 actual、日期误识别或内容串线。"
                ),
            }

    return {"status": "ok", "reason": ""}
