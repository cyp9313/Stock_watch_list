# -*- coding: utf-8 -*-
"""Materiality Ranking（修改计划第六轮第 16 节）。

对每条候选证据计算 6 个评分分量，加权得到 selection_score，并应用硬过滤。

selection_score = primary_entity_score * 0.25
                + materiality_score * 0.20
                + recency_score * 0.20
                + portfolio_impact_score * 0.15
                + source_authority_score * 0.15
                + novelty_score * 0.05

硬过滤（任一命中即 reject）：
- primary_entity_score < 0.70
- materiality_score < 0.40
- decision_usefulness_score < 0.35
- quote_page == true
- incidental mention == true
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from .source_lanes import classify_source, source_authority_score
from .entity_resolution import resolve_primary_entity


# 硬过滤阈值（修改计划第 16 / 21 节，可通过环境变量调整）
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


MIN_PRIMARY_ENTITY_SCORE = _env_float("PORTFOLIO_NEWS_MIN_PRIMARY_ENTITY_SCORE", 0.70)
MIN_MATERIALITY_SCORE = _env_float("PORTFOLIO_NEWS_MIN_MATERIALITY_SCORE", 0.40)
MIN_DECISION_USEFULNESS_SCORE = 0.35


# 重大事件关键词（materiality_score 加分）
_MATERIAL_EVENT_PATTERNS = [
    r"\b(downgrad\w*|upgrade\w*|cut|raise|lower)\b.*\b(rating|target|price target)\b",
    r"\b(earnings|results|revenue|guidance)\b.*\b(beat|miss|raise|lower|cut)\b",
    r"\b(lawsuit|litigation|probe|investigation|sec|doj|regulator)\b",
    r"\b(recall|safety|defect|breach|cyber)\b",
    r"\b(merger|acquisition|deal|buyout|takeover)\b",
    r"\b(bankrupt\w*|restructur\w*|chapter 11)\b",
    r"\b(dividend|buyback|share repurchase)\b.*\b(cut|suspend|increase|authorize)\b",
    r"\b(fda|ema)\b.*\b(approval|reject|delay)\b",
    r"\b(downgrade\w*|default\w*)\b.*\b(credit|rating|debt)\b",
    r"\b(capex|capital expenditure|financing|debt offering|bond)\b",
    r"\b(CEO|CFO|CTO|president|chairman)\b.*\b(resign\w*|step down|appoint\w*|fire\w*)\b",
    r"\b(supply|outage|disruption|shortage|force majeure)\b",
    r"\b(policy|regulation|ban|approval|license)\b",
    r"\b(production|deliveries|output)\b.*\b(cut|increase|miss|beat)\b",
]

# 决策无用关键词（decision_usefulness_score 降分）
_LOW_DECISION_HINTS = [
    "summary", "overview", "what is", "how to", "explained",
    "beginner", "guide", "tutorial", "definition", "meaning",
    "vs", "versus", "comparison", "alternatives",
]


def _recency_score(published_date: str, *, fresh_days: int = 45, background_days: int = 120) -> float:
    """时效评分：fresh_event=1.0, recent_background=0.7, structural=0.4, stale=0.1, unknown=0.2。"""
    s = str(published_date or "").strip()
    if not s or len(s) < 10:
        return 0.2
    try:
        d = date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return 0.2
    age = (date.today() - d).days
    if age <= fresh_days:
        return 1.0
    if age <= background_days:
        return 0.7
    if age <= 365:
        return 0.4
    return 0.1


def _materiality_score(title: str, summary: str, body: str) -> float:
    """重大性评分：基于事件关键词匹配。"""
    text = " ".join([title or "", summary or "", body or ""]).lower()
    if not text.strip():
        return 0.2
    hits = sum(1 for pat in _MATERIAL_EVENT_PATTERNS if re.search(pat, text, re.I))
    # 0 命中 → 0.2；1 命中 → 0.6；2 命中 → 0.8；3+ → 0.95
    if hits == 0:
        return 0.25
    if hits == 1:
        return 0.60
    if hits == 2:
        return 0.80
    return 0.95


def _portfolio_impact_score(
    ticker: str | None,
    meta: dict[str, Any],
    ranking: dict[str, Any],
    metrics: dict[str, Any],
) -> float:
    """组合影响评分：基于 ticker 的风险贡献占比。"""
    if not ticker:
        return 0.5  # 宏观
    rc_map = {item.get("ticker"): item for item in metrics.get("risk_contributions", []) or []}
    rc = float((rc_map.get(ticker) or {}).get("risk_contribution") or 0.0)
    total_rc = sum(float(it.get("risk_contribution") or 0.0) for it in metrics.get("risk_contributions", []) or [])
    if total_rc <= 0:
        return 0.5
    ratio = rc / total_rc
    # ratio >= 0.15 → 1.0; 0.10-0.15 → 0.8; 0.05-0.10 → 0.6; <0.05 → 0.4
    if ratio >= 0.15:
        return 1.0
    if ratio >= 0.10:
        return 0.8
    if ratio >= 0.05:
        return 0.6
    return 0.4


def _decision_usefulness_score(title: str, summary: str, body: str, page_classification: str) -> float:
    """决策有用性评分：quote/reference page 严重降分；含低决策词降分。"""
    if page_classification in {"quote_page"}:
        return 0.10
    if page_classification in {"reference_page"}:
        return 0.25
    text = " ".join([title or "", summary or "", body or ""]).lower()
    if any(h in text for h in _LOW_DECISION_HINTS):
        return 0.30
    # 含明确事件词 → 高分
    if any(re.search(pat, text, re.I) for pat in _MATERIAL_EVENT_PATTERNS):
        return 0.85
    return 0.55


def _novelty_score(question_id: str | None, seen_event_keys: set[str], event_key: str | None) -> float:
    """新颖性评分：未见过的事件 → 1.0；已见过 → 0.3。"""
    if not event_key:
        return 0.5
    if event_key in seen_event_keys:
        return 0.30
    return 1.0


# selection_score 权重（修改计划第 16 节）
_WEIGHTS = {
    "primary_entity": 0.25,
    "materiality": 0.20,
    "recency": 0.20,
    "portfolio_impact": 0.15,
    "source_authority": 0.15,
    "novelty": 0.05,
}


def rank_evidence(
    result: dict[str, Any],
    *,
    ticker: str | None,
    meta: dict[str, Any],
    ranking: dict[str, Any],
    metrics: dict[str, Any],
    body: str = "",
    seen_event_keys: set[str] | None = None,
    event_key: str | None = None,
) -> dict[str, Any]:
    """对单条候选证据计算完整评分。

    返回结构（修改计划第 16 节）：
        {
            "primary_entity_score": float,
            "materiality_score": float,
            "recency_score": float,
            "portfolio_impact_score": float,
            "decision_usefulness_score": float,
            "source_authority_score": float,
            "novelty_score": float,
            "selection_score": float,
            "entity_role": "primary"|"incidental",
            "page_classification": str,
            "is_quote_page": bool,
            "is_reference_page": bool,
            "accepted": bool,
            "reject_reason": str | None,
            "entity_resolution": {...},  # 完整 entity_resolution 输出
        }
    """
    seen_event_keys = seen_event_keys or set()
    title = str(result.get("title") or "")
    summary = str(result.get("summary") or "")
    url = str(result.get("url") or "")
    domain = urlparse(url).netloc.lower().removeprefix("www.") if url else ""

    # Entity resolution
    entity = resolve_primary_entity(result, ticker, meta, body=body)

    # 各分量评分
    primary_entity_score = entity["primary_entity_score"]
    materiality_score = _materiality_score(title, summary, body)
    recency_score = _recency_score(result.get("published_date"))
    portfolio_impact_score = _portfolio_impact_score(ticker, meta, ranking, metrics)
    decision_usefulness = _decision_usefulness_score(title, summary, body, entity["page_classification"])
    src_class = classify_source(
        domain,
        official_domains=meta.get("official_domains") or [],
        regulator_domains=meta.get("regulator_domains") or [],
    )
    source_authority = src_class["authority_score"] / 100.0
    novelty_score = _novelty_score(result.get("question_id"), seen_event_keys, event_key)

    # selection_score 加权
    selection_score = (
        primary_entity_score * _WEIGHTS["primary_entity"]
        + materiality_score * _WEIGHTS["materiality"]
        + recency_score * _WEIGHTS["recency"]
        + portfolio_impact_score * _WEIGHTS["portfolio_impact"]
        + source_authority * _WEIGHTS["source_authority"]
        + novelty_score * _WEIGHTS["novelty"]
    )

    # 硬过滤（修改计划第 16 节）
    reject_reason: str | None = None
    if entity["is_quote_page"]:
        reject_reason = "quote_page"
    elif entity["entity_role"] == "incidental":
        reject_reason = "incidental_entity_mention"
    elif primary_entity_score < MIN_PRIMARY_ENTITY_SCORE:
        reject_reason = f"primary_entity_score_below_{MIN_PRIMARY_ENTITY_SCORE}"
    elif materiality_score < MIN_MATERIALITY_SCORE:
        reject_reason = f"materiality_score_below_{MIN_MATERIALITY_SCORE}"
    elif decision_usefulness < MIN_DECISION_USEFULNESS_SCORE:
        reject_reason = f"decision_usefulness_below_{MIN_DECISION_USEFULNESS_SCORE}"

    return {
        "primary_entity_score": round(primary_entity_score, 3),
        "materiality_score": round(materiality_score, 3),
        "recency_score": round(recency_score, 3),
        "portfolio_impact_score": round(portfolio_impact_score, 3),
        "decision_usefulness_score": round(decision_usefulness, 3),
        "source_authority_score": round(source_authority, 3),
        "novelty_score": round(novelty_score, 3),
        "selection_score": round(selection_score, 3),
        "entity_role": entity["entity_role"],
        "page_classification": entity["page_classification"],
        "is_quote_page": entity["is_quote_page"],
        "is_reference_page": entity["is_reference_page"],
        "accepted": reject_reason is None,
        "reject_reason": reject_reason,
        "entity_resolution": entity,
    }
