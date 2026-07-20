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

# §18 修复：Event Type-specific materiality 加分模式
# 仅当证据的 event_hint 匹配时，额外计入这些多词规则。
# 未匹配的模式在基础分中仍可能命中，但 event-specific 命中权重更高（×1.5）。
_EVENT_SPECIFIC_PATTERNS: dict[str, list[str]] = {
    "earnings_date": [
        r"\b(earnings\s+date|report\s+date|fiscal\s+quarter)\b.*\b\d{4}-\d{2}-\d{2}\b",
        r"\b(earnings\s+calendar|upcoming\s+earnings)\b",
    ],
    "earnings_results": [
        r"\b(reported|posted|announced)\b.*\b(revenue|eps|earnings|profit|loss)\b.*\b(\$?\d+(?:\.\d+)?\s*[bm])\b",
        r"\b(revenue|eps|earnings)\b.*\b(increased|decreased|grew|fell|rose|dropped)\b.*\b\d+%",
        r"\b(beat|missed|met|exceeded)\b.*\b(estimate|expectation|consensus|forecast)\b",
        r"\b(q[1-4]|fy\d{2})\b.*\b(results?|earnings?|numbers?)\b",
    ],
    "guidance": [
        r"\b(raised|lowered|reaffirmed|updated|issued|withdrew)\b.*\b(guidance|outlook|forecast)\b",
        r"\b(sees|expects|projects|anticipates)\b.*\b(revenue|eps|growth|margin)\b.*\b\d{4}\b",
    ],
    "credit_and_financing": [
        r"\b(moody'?s?|s&p|fitch|morningstar)\b.*\b(downgrade|upgrade|outlook|rating|affirm)\b",
        r"\b(credit\s+rating|bond\s+rating|issuer\s+rating)\b",
        r"\b(debt|bond|note|offering|facility|revolving)\b.*\b(\$?\d+(?:\.\d+)?\s*[bm])\b",
        r"\b(refinanc\w*|maturit\w*|covenant|leverage|interest\s+coverage)\b",
    ],
    "merger_acquisition": [
        r"\b(acquir\w*|buy|purchase|merge)\b.*\b(\$?\d+(?:\.\d+)?\s*[bm])\b",
        r"\b(definitive\s+agreement|letter\s+of\s+intent|binding\s+offer)\b",
    ],
    "management_change": [
        r"\b(CEO|CFO|CTO|COO|president|chairman|director)\b.*\b(resign\w*|step\s+down|appoint\w*|named|hire\w*)\b",
    ],
    "theme_supply": [
        r"\b(mine|mining|production|output|capacity|expansion)\b.*\b(outage|disruption|halt|shutdown|restart|increase)\b",
        r"\b(supply|inventory|stockpile)\b.*\b(deficit|shortage|surplus|tight|glut)\b",
        r"\b(agreement|contract|deal|approval|permit|license)\b.*\b(government|regulator|ministry)\b",
        r"\b(uranium|nuclear|enrichment|reactor|waste)\b.*\b(plant|facility|capacity|production)\b",
    ],
    "theme_policy": [
        r"\b(regulation|policy|legislation|bill|directive|mandate)\b.*\b(passed|approved|proposed|introduced|implement)\b",
        r"\b(tariff|sanction|ban|restriction|subsidy|tax\s+credit)\b",
    ],
    "regulatory": [
        r"\b(fine|penalty|settlement|consent\s+order|cease\s+and\s+desist)\b",
        r"\b(DOJ|SEC|FTC|CFPB|FCA|ESMA)\b.*\b(investigat\w*|charge\w*|sue\w*|action)\b",
    ],
    "crypto_regulation": [
        r"\b(SEC|CFTC|EU|MiCA)\b.*\b(approv\w*|reject\w*|delay|file\w*)\b.*\b(ETF|crypto|bitcoin)\b",
        r"\b(exchange|platform)\b.*\b(ban|restrict|license|register)\b",
    ],
}

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


def _materiality_score(title: str, summary: str, body: str, *, event_hint: str = "") -> float:
    """重大性评分：通用模式 + Event Type-specific 双轨（§18 修复）。

    - 通用模式命中：基础分
    - Event-specific 模式命中：权重 ×1.5
    - 0 命中=0.25, 1=0.60, 2=0.80, 3+=0.95
    """
    text = " ".join([title or "", summary or "", body or ""]).lower()
    if not text.strip():
        return 0.2

    generic_hits = sum(1 for pat in _MATERIAL_EVENT_PATTERNS if re.search(pat, text, re.I))

    # Event-specific bonuses (§18)
    specific_hits = 0
    if event_hint and event_hint in _EVENT_SPECIFIC_PATTERNS:
        specific_patterns = _EVENT_SPECIFIC_PATTERNS[event_hint]
        specific_hits = sum(1 for pat in specific_patterns if re.search(pat, text, re.I))

    weighted_hits = generic_hits + int(specific_hits * 1.5)
    if weighted_hits == 0:
        return 0.25
    if weighted_hits <= 1:
        return 0.60
    if weighted_hits <= 2:
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
    event_hint = str(result.get("event_hint") or result.get("event_need") or "")
    materiality_score = _materiality_score(title, summary, body, event_hint=event_hint)
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
        "source_type": str(src_class.get("source_type") or "unknown"),
        "source_is_official": bool(src_class.get("is_official")),
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
