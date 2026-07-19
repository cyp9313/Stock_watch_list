# -*- coding: utf-8 -*-
"""统一 Evidence 身份与收口工具（第七轮修改计划第 3 节）。

核心不变量：
- ``evidence_uid``：全流程稳定主键（sha256），不因排序/补搜改变，用于内部关联、去重、缓存；
- ``evidence_id``：最终报告显示编号，只在收口时一次性分配给 *accepted* 证据，从 E001 起；
- rejected / reference 证据 ``evidence_id`` 必须为 ``None``。

设计目标：消除"子流程自行从 E001 编号 → 补搜重复 ID → 摘要串线"的根因。
"""
from __future__ import annotations

import hashlib
from typing import Any


def _norm(value: Any) -> str:
    return ("" if value is None else str(value)).strip().lower()


def make_evidence_uid(note: dict[str, Any]) -> str:
    """基于稳定业务字段生成证据主键（不因排序或补搜变化）。"""
    url = _norm(note.get("url"))
    ticker = _norm(note.get("ticker"))
    date = _norm(note.get("published_date") or note.get("event_date"))
    title = _norm(note.get("title") or note.get("raw_title"))
    key = "\n".join([url, ticker, date, title])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return "ev_" + digest


# accepted 判定所需字段（合并修改计划第 6.1 节与现有 recency/verification 门）。
_ACCEPTED_ENTITY_ROLES = {"primary", "theme_primary"}
_ACCEPTED_RECENCY_TIERS = {"fresh_event", "recent_background"}


def is_accepted_evidence(item: dict[str, Any]) -> bool:
    """判断一条证据是否满足"进入正式报告"的全部硬条件。

    P0-3 修复：accept 缺失默认 reject（fail-closed），chronology_conflict 强制 reject。
    """
    if not item.get("materiality_accepted"):
        return False
    # P0-3: 显式接受 — 缺失 accept 或 accept!=True → reject
    if item.get("accept") is not True:
        return False
    # P0-3: 时序冲突 → 直接 reject
    if item.get("chronology_conflict"):
        return False
    if item.get("entity_role") not in _ACCEPTED_ENTITY_ROLES:
        return False
    if item.get("is_quote_page"):
        return False
    if item.get("is_reference_page"):
        return False
    if str(item.get("recency_tier") or "") == "stale":
        return False
    if item.get("recency_tier") not in _ACCEPTED_RECENCY_TIERS:
        return False
    if not (item.get("article_fetch_ok") or item.get("snippet_fallback_ok")):
        return False
    return True


def finalize_evidence_ids(evidence: list[dict[str, Any]]) -> None:
    """收口：仅为 accepted 证据分配显示编号 E001..，其余置 ``None``。

    必须在 Decision Summarizer 运行之后、质量门与 Agent 之前调用。
    """
    for item in evidence:
        item["evidence_id"] = None
    accepted = sorted(
        [item for item in evidence if is_accepted_evidence(item)],
        key=lambda e: float(e.get("priority_score") or 0.0),
        reverse=True,
    )
    for idx, item in enumerate(accepted, start=1):
        item["evidence_id"] = f"E{idx:03d}"


def validate_evidence_identity(evidence: list[dict[str, Any]]) -> list[str]:
    """返回身份完整性错误列表（空列表表示通过）。

    检查：
    - ``evidence_uid`` 全局唯一；
    - 非 None 的 ``evidence_id`` 在集合内唯一。
    """
    errors: list[str] = []
    uids = [item.get("evidence_uid") for item in evidence if item.get("evidence_uid")]
    if len(uids) != len(set(uids)):
        seen: set[str] = set()
        dups: set[str] = set()
        for uid in uids:
            if uid in seen:
                dups.add(uid)
            seen.add(uid)
        errors.append("duplicate_evidence_uid:" + ",".join(sorted(dups)))
    ids = [item.get("evidence_id") for item in evidence if item.get("evidence_id")]
    if len(ids) != len(set(ids)):
        seen = set()
        dups = set()
        for eid in ids:
            if eid in seen:
                dups.add(eid)
            seen.add(eid)
        errors.append("duplicate_evidence_id:" + ",".join(sorted(dups)))
    return errors


def split_evidence_groups(evidence: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """将证据分为 accepted / diagnostic_rejected / reference 三组。"""
    accepted: list[dict[str, Any]] = []
    reference: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in evidence:
        if is_accepted_evidence(item):
            accepted.append(item)
        elif item.get("is_reference_page") or str(item.get("page_classification") or "") == "reference":
            reference.append(item)
        else:
            rejected.append(item)
    return {"accepted": accepted, "diagnostic_rejected": rejected, "reference": reference}
