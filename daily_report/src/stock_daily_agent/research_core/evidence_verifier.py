# -*- coding: utf-8 -*-
"""Evidence Verifier（修改计划第六轮第 20 节）。

重构验证标签：不再仅凭 article_fetch_ok=true 就写「正文已验证」。

输出：
    body_fetch_status: ok | failed | not_attempted
    body_text_quality: high | medium | low | unknown
    source_authenticity: official | regulator | rating_agency | major_media | specialty | aggregator | unknown
    corroboration_count: int  # 多少个独立来源报道同一事件
    verification_level: primary_source | regulatory_filing | major_media_body_extracted |
                        multi_source_corroborated | single_source | search_snippet

HTML 标签映射：
    primary_source → 官方原文
    regulatory_filing → 监管文件
    major_media_body_extracted → 主流媒体正文已提取
    multi_source_corroborated → 多源交叉确认
    single_source → 单一来源
    search_snippet → 搜索摘要
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .source_lanes import classify_source


VERIFICATION_LEVELS = {
    "primary_source": "官方原文",
    "regulatory_filing": "监管文件",
    "major_media_body_extracted": "主流媒体正文已提取",
    "multi_source_corroborated": "多源交叉确认",
    "single_source": "单一来源",
    "search_snippet": "搜索摘要",
}


def _body_fetch_status(ev: dict[str, Any]) -> str:
    if ev.get("article_fetch_ok"):
        return "ok"
    if ev.get("article_fetch_error"):
        return "failed"
    return "not_attempted"


def _body_text_quality(ev: dict[str, Any]) -> str:
    if not ev.get("article_fetch_ok"):
        return "unknown"
    facts = ev.get("facts") or []
    total_chars = sum(len(str(f)) for f in facts)
    if total_chars >= 300:
        return "high"
    if total_chars >= 120:
        return "medium"
    return "low"


def _source_authenticity(ev: dict[str, Any], meta: dict[str, Any]) -> str:
    domain = ev.get("source_domain") or ""
    src = classify_source(
        domain,
        official_domains=meta.get("official_domains") or [],
        regulator_domains=meta.get("regulator_domains") or [],
    )
    return src["source_type"]


def verify_evidence(
    ev: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
    corroboration_count: int = 0,
) -> dict[str, Any]:
    """计算单条证据的验证级别。

    corroboration_count 由 event_clusterer 提供（同一 event_key 的来源数）。
    """
    meta = meta or {}
    body_status = _body_fetch_status(ev)
    body_quality = _body_text_quality(ev)
    src_auth = _source_authenticity(ev, meta)

    # 决定 verification_level（修改计划第 20 节优先级）
    level = "search_snippet"
    if body_status == "ok":
        if src_auth == "official":
            level = "primary_source"
        elif src_auth == "regulator":
            level = "regulatory_filing"
        elif src_auth == "major_media" and body_quality in {"high", "medium"}:
            level = "major_media_body_extracted"
        elif corroboration_count >= 2:
            level = "multi_source_corroborated"
        else:
            level = "single_source"
    else:
        # 未抓取正文：仅当多源且搜索摘要时才算 multi_source_corroborated
        if corroboration_count >= 3:
            level = "multi_source_corroborated"
        else:
            level = "search_snippet"

    return {
        "body_fetch_status": body_status,
        "body_text_quality": body_quality,
        "source_authenticity": src_auth,
        "corroboration_count": corroboration_count,
        "verification_level": level,
        "verification_level_zh": VERIFICATION_LEVELS.get(level, level),
    }


def compute_corroboration_counts(evidence: list[dict[str, Any]]) -> dict[str, int]:
    """按 event_key 统计每个事件的独立来源数。"""
    counts: dict[str, int] = {}
    for ev in evidence:
        key = str(ev.get("event_key") or ev.get("evidence_id") or "")
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts
