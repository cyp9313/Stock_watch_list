# -*- coding: utf-8 -*-
"""Event Clustering（修改计划第六轮第 17 节）。

同一事件只保留一组。字段：
    {
        "event_key": "TSLA_2026_Q2_EARNINGS_SETUP",
        "event_type": "earnings_date",
        "event_date": "2026-07-17",
        "primary_source_id": "E001",
        "supporting_source_ids": ["E004"]
    }

来源优先级（primary 选择）：官方 > 监管 > 评级机构 > Reuters/Bloomberg/AP >
专业媒体 > 二次媒体 > 聚合站。

不能让同一个 Q1 earnings 占据两到三条 Evidence。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .source_lanes import classify_source
from .event_normalizer import normalize_event


# 来源优先级评分（用于 primary_source 选择）
_SOURCE_PRIORITY: dict[str, int] = {
    "official": 100,
    "regulator": 95,
    "rating_agency": 90,
    "major_media": 80,
    "specialty_media": 65,
    "aggregator": 40,
    "unknown": 30,
}


def _source_priority(domain: str | None, official_domains: list[str] | None) -> int:
    src = classify_source(domain, official_domains=official_domains or [])
    return _SOURCE_PRIORITY.get(src["source_type"], 30)


def _normalize_title_key(title: str) -> str:
    """归一化标题用于事件相似度比较。"""
    s = (title or "").lower()
    # 去除常见噪声词
    s = re.sub(r"\b(breaking|update|report|news|says|said|announces|announced)\b", "", s)
    # 去除日期
    s = re.sub(r"\b(20\d{2})\b", "", s)
    s = re.sub(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b", "", s)
    s = re.sub(r"\b(q[1-4])\b", "", s)
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_similarity(a: str, b: str) -> float:
    """简单 Jaccard 相似度（基于 token 集合）。"""
    ta = set(_normalize_title_key(a).split())
    tb = set(_normalize_title_key(b).split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union > 0 else 0.0


def _build_event_key(ticker: str | None, event_type: str, event_date: str | None, title: str) -> str:
    """构建 event_key，用于跨证据去重。"""
    parts = []
    if ticker:
        parts.append(ticker.upper().replace("-", "_").replace(".", "_"))
    else:
        parts.append("MACRO")
    if event_date:
        parts.append(event_date.replace("-", "_"))
    # 从标题提取关键 token（前 3 个非噪声词）
    key_title = _normalize_title_key(title)
    tokens = [t for t in key_title.split() if len(t) >= 4][:3]
    if tokens:
        parts.append("_".join(tokens).upper())
    parts.append(event_type.upper())
    return "_".join(parts)


def annotate_event_identity(
    evidence: list[dict[str, Any]],
    *,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """在 Materiality/Novelty 评分前确定 event_key，不执行去重。"""
    instrument_metadata = instrument_metadata or {}
    annotated: list[dict[str, Any]] = []
    for item in evidence or []:
        event = dict(item)
        ticker = event.get("ticker")
        event_info = normalize_event(
            event, ticker=ticker,
            event_need=event.get("event_hint") or event.get("event_need") or event.get("event_type"),
        )
        event["event_type"] = event_info["event_type"]
        event["event_date"] = event_info["event_date"]
        event["event_key"] = _build_event_key(
            ticker, event_info["event_type"], event_info["event_date"], event.get("title") or "",
        )
        annotated.append(event)
    return annotated


def cluster_events(
    evidence: list[dict[str, Any]],
    *,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
    similarity_threshold: float = 0.55,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """对 evidence 列表做事件聚类去重。

    返回 (clustered_evidence, event_clusters)：
    - clustered_evidence：每个 cluster 只保留 primary_source，其余作为 supporting
    - event_clusters：聚类元信息列表
    """
    instrument_metadata = instrument_metadata or {}
    if not evidence:
        return [], []

    # 为每条 evidence 构建事件信息
    annotated: list[dict[str, Any]] = []
    for idx, ev in enumerate(evidence):
        ticker = ev.get("ticker")
        meta = instrument_metadata.get(ticker, {}) or {}
        event_info = normalize_event(ev, ticker=ticker, event_need=ev.get("event_hint") or ev.get("event_need"))
        domain = ev.get("source_domain") or ""
        priority = _source_priority(domain, meta.get("official_domains"))
        event_key = _build_event_key(ticker, event_info["event_type"], event_info["event_date"], ev.get("title") or "")
        annotated.append({
            "idx": idx,
            "ev": ev,
            "event_info": event_info,
            "event_key": event_key,
            "source_priority": priority,
            "source_domain": domain,
            "official_domains": meta.get("official_domains") or [],
        })

    # 聚类：相同 event_key 直接合并；不同 event_key 但标题相似度 >= threshold 也合并
    clusters: list[list[dict[str, Any]]] = []
    used: set[int] = set()
    for i, a in enumerate(annotated):
        if a["idx"] in used:
            continue
        cluster = [a]
        used.add(a["idx"])
        for j in range(i + 1, len(annotated)):
            b = annotated[j]
            if b["idx"] in used:
                continue
            # 同 ticker + 同 event_key → 合并
            same_key = (a["event_key"] == b["event_key"]) and (a["event_info"]["ticker"] == b["event_info"]["ticker"])
            # 同 ticker + 同 event_type + 同日期 + 标题相似 → 合并
            same_event = (
                a["event_info"]["ticker"] == b["event_info"]["ticker"]
                and a["event_info"]["event_type"] == b["event_info"]["event_type"]
                and a["event_info"]["event_date"]
                and a["event_info"]["event_date"] == b["event_info"]["event_date"]
                and _title_similarity(a["ev"].get("title") or "", b["ev"].get("title") or "") >= similarity_threshold
            )
            # 同 ticker + 标题高度相似 → 合并（即便日期不同）
            high_sim = (
                a["event_info"]["ticker"] == b["event_info"]["ticker"]
                and _title_similarity(a["ev"].get("title") or "", b["ev"].get("title") or "") >= 0.75
            )
            if same_key or same_event or high_sim:
                cluster.append(b)
                used.add(b["idx"])
        clusters.append(cluster)

    # 每个 cluster 选 primary_source（来源优先级最高），其余为 supporting
    clustered_evidence: list[dict[str, Any]] = []
    event_clusters: list[dict[str, Any]] = []
    for c_idx, cluster in enumerate(clusters, start=1):
        # 按 source_priority 降序，primary 取第一个
        cluster.sort(key=lambda x: x["source_priority"], reverse=True)
        primary = cluster[0]
        supporting = cluster[1:]
        primary_ev = dict(primary["ev"])
        primary_ev["event_key"] = primary["event_key"]
        primary_ev["event_type"] = primary["event_info"]["event_type"]
        primary_ev["event_date"] = primary["event_info"]["event_date"]
        primary_ev["cluster_id"] = c_idx
        primary_ev["is_primary_source"] = True
        primary_ev["supporting_source_ids"] = [s["ev"].get("evidence_id") for s in supporting if s["ev"].get("evidence_id")]
        clustered_evidence.append(primary_ev)

        event_clusters.append({
            "cluster_id": c_idx,
            "event_key": primary["event_key"],
            "event_type": primary["event_info"]["event_type"],
            "event_date": primary["event_info"]["event_date"],
            "ticker": primary["event_info"]["ticker"],
            "primary_source_id": primary["ev"].get("evidence_id"),
            "primary_source_domain": primary["source_domain"],
            "primary_source_priority": primary["source_priority"],
            "supporting_source_ids": [s["ev"].get("evidence_id") for s in supporting if s["ev"].get("evidence_id")],
            "supporting_source_domains": [s["source_domain"] for s in supporting],
            "cluster_size": len(cluster),
        })

    return clustered_evidence, event_clusters
