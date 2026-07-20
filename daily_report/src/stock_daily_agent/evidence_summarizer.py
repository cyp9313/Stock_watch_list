# -*- coding: utf-8 -*-
"""One-pass Chinese evidence summarization without generating trade advice."""
from __future__ import annotations

import json
import os
from typing import Any

from .config import build_llm_cfg


def _content(response: Any) -> str:
    if isinstance(response, list):
        return _content(response[-1]) if response else ""
    if isinstance(response, dict):
        value = response.get("content", "")
    else:
        value = getattr(response, "content", "")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(x.get("text") or x.get("content") or "") for x in value if isinstance(x, dict))
    return str(value or "")


def _parse_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Evidence summarizer did not return JSON")
    result = json.loads(text[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError("Evidence summarizer output must be an object")
    return result


def summarize_evidence_zh(
    evidence: list[dict[str, Any]],
    instrument_metadata: dict[str, dict[str, Any]],
    *,
    model: str,
    provider: str,
    report_date: str = "",
) -> dict[str, Any]:
    """Decision Evidence Summarizer（修改计划第六轮第 19 节 + §19 report_date）。"""
    if not evidence:
        return {"status": "no_evidence", "evidence": evidence, "errors": []}
    provider = (provider or "dashscope").lower()
    if provider == "dashscope" and not os.environ.get("DASHSCOPE_API_KEY"):
        _quarantine_all(evidence, "summarizer_not_configured")
        return {
            "status": "not_configured", "evidence": evidence,
            "errors": ["DASHSCOPE_API_KEY not configured"],
            **_build_isolation_diagnostics(evidence),
        }
    if provider == "deepseek" and not (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        _quarantine_all(evidence, "summarizer_not_configured")
        return {
            "status": "not_configured", "evidence": evidence,
            "errors": ["DeepSeek API key not configured"],
            **_build_isolation_diagnostics(evidence),
        }
    if provider == "openai_compatible" and not (os.environ.get("OPENAI_API_KEY") or os.environ.get("QWEN_API_KEY")):
        _quarantine_all(evidence, "summarizer_not_configured")
        return {
            "status": "not_configured", "evidence": evidence,
            "errors": ["OpenAI-compatible API key not configured"],
            **_build_isolation_diagnostics(evidence),
        }

    # 只有真正调用本轮 Summarizer 时才清空旧决策；provider 不可用时，
    # 新管线 Evidence 因 accept 缺失仍会 Fail-closed，显式 legacy accept 可保持兼容。
    for item in evidence:
        item["accept"] = False
        item.pop("decision_rejected", None)

    # 先用 evidence_verifier 计算 verification_level（确定性，不依赖 LLM）
    from .research_core.evidence_verifier import verify_evidence, compute_corroboration_counts
    corroboration = compute_corroboration_counts(evidence)
    for item in evidence:
        ticker = item.get("ticker")
        meta = instrument_metadata.get(ticker, {}) if ticker else {}
        key = str(item.get("event_key") or item.get("evidence_uid") or "")
        vinfo = verify_evidence(item, meta=meta, corroboration_count=corroboration.get(key, 0))
        item["body_fetch_status"] = vinfo["body_fetch_status"]
        item["body_text_quality"] = vinfo["body_text_quality"]
        item["source_authenticity"] = vinfo["source_authenticity"]
        item["corroboration_count"] = vinfo["corroboration_count"]
        item["verification_level"] = vinfo["verification_level"]
        item["verification_level_zh"] = vinfo["verification_level_zh"]

        # §15 修复：财报时间线校验（forecast 不得被当 actual）
        from .research_core.chronology_validator import check_earnings_chronology
        chrono = check_earnings_chronology(item, instrument_meta=meta)
        if chrono["status"] != "ok":
            item["chronology_status"] = chrono["status"]
            item["chronology_reason"] = chrono["reason"]
            if chrono["status"] == "chronology_conflict":
                # 时间冲突 → reject 或降级置信度
                item["chronology_conflict"] = True
                item["confidence"] = min(item.get("confidence", 0.5), 0.30)
            elif chrono["status"] == "likely_forecast":
                item["content_type"] = item.get("content_type") or "forecast"

    payload = []
    for item in evidence:
        ticker = item.get("ticker")
        payload.append({
            "evidence_uid": item.get("evidence_uid"),
            "display_id": None,
            "ticker": ticker,
            "instrument": instrument_metadata.get(ticker, {}) if ticker else {},
            "title": item.get("title") or item.get("raw_title"),
            "snippet": item.get("summary") or item.get("raw_snippet"),
            "article_facts": item.get("facts") or [],
            "source": item.get("source_name"),
            "date": item.get("published_date") or item.get("raw_published_date"),
            "content_basis": item.get("content_basis"),
            "article_fetch_ok": bool(item.get("article_fetch_ok")),
            "verification_level": item.get("verification_level"),
            "entity_role": item.get("entity_role"),
            "materiality_score": item.get("materiality_score"),
            "event_type": item.get("event_type") or item.get("event_hint"),
        })
    prompt = (
        "你是决策证据摘要器，只能基于输入事实做忠实中文摘要，不得生成投资或交易建议，不得补充输入中没有的事实。\n"
        + (f"报告日期：{report_date}。日期早晚判断必须以报告日期为准，不得使用模型自身认知的当前日期。\n" if report_date else "")
        + "article_fetch_ok=false 或 verification_level=search_snippet 表示内容仅来自搜索结果摘要，必须按未验证线索表述。\n\n"
        "对每条证据返回严格 JSON：\n"
        "{\"items\":[{\"evidence_uid\":\"ev_...\",\"accept\":true|false,\"reject_reason\":null|\"incidental_entity_mention\"|\"quote_page\"|\"stale\"|\"low_materiality\",\n"
        "\"event_title_zh\":\"事件标题中文\",\"what_happened_zh\":\"发生了什么\",\"what_changed_zh\":\"本轮新增变化\",\n"
        "\"why_it_matters_to_ticker_zh\":\"为什么影响标的\",\"portfolio_impact_zh\":\"为什么影响当前持仓\",\n"
        "\"supports_action\":\"reduce|trim|hold|watch|add|none\",\n"
        "\"does_not_prove_zh\":\"该证据不能单独证明什么\",\n"
        "\"impact_direction\":\"positive|negative|mixed|neutral|unknown\",\n"
        "\"impact_horizon\":\"short|medium|long\",\"confidence\":0.0}]}\n\n"
        "规则：\n"
        "- 如果 entity_role=incidental 或 page_classification=quote_page，accept=false；\n"
        "- supports_action 仅表示该证据倾向支持哪个方向，不是最终建议；\n"
        "- does_not_prove_zh 必须明确该证据的局限性；\n"
        "- 不得编造事件或因果关系。\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        from qwen_agent.llm import get_chat_model
        llm = get_chat_model(build_llm_cfg(model=model, provider=provider))
        response = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            extra_generate_cfg={"temperature": 0.1},
        )
        parsed = _parse_json(_content(response))
        summary_errors = _apply_summaries_by_uid(evidence, parsed.get("items", []))
        covered = sum(1 for item in evidence if item.get("summary_method") == "llm_decision_summarizer")
        accepted = sum(1 for item in evidence if item.get("accept") is True)
        isolation = _build_isolation_diagnostics(evidence)
        return {
            "status": "success" if covered == len(evidence) else "partial",
            "evidence": evidence,
            "summarized_count": covered,
            "accepted_count": accepted,
            "rejected_count": len(evidence) - accepted,
            "errors": list(summary_errors),
            **isolation,
        }
    except Exception as exc:  # noqa: BLE001
        _quarantine_all(evidence, "summarizer_provider_error")
        return {
            "status": "provider_error",
            "evidence": evidence,
            "summarized_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "errors": [f"summarizer_failed: {type(exc).__name__}: {exc}"],
            **_build_isolation_diagnostics(evidence),
        }


# 第七轮第 4 节：AI 摘要器只能补充解释性字段，不得修改原始身份字段。
_IMMUTABLE_FIELDS = ("ticker", "url", "source_domain", "published_date", "event_key", "raw_title")


def _quarantine_all(evidence: list[dict[str, Any]], reason: str) -> None:
    for item in evidence or []:
        item["accept"] = False
        item["summary_integrity_ok"] = False
        item["summary_isolation_reason"] = reason


def validate_summary_identity(original: dict[str, Any], summarized: dict[str, Any]) -> None:
    """断言摘要未篡改原始身份字段；违规抛 AssertionError。"""
    assert summarized.get("evidence_uid") == original.get("evidence_uid"), "evidence_uid 不一致"
    for field in _IMMUTABLE_FIELDS:
        if field in summarized and summarized.get(field) is not None:
            assert summarized.get(field) == original.get(field), f"{field} 被篡改"


def _apply_summaries_by_uid(
    evidence: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[str]:
    """按 ``evidence_uid`` Fail-closed 应用摘要结果。"""
    errors: list[str] = []
    # Integrity is tracked per Evidence rather than treating one malformed LLM item
    # as a reason to abort the whole report.  Every item starts quarantined and is
    # promoted only after UID, immutable identity and explicit ``accept`` checks pass.
    for item in evidence:
        item["summary_integrity_ok"] = False
        item["summary_isolation_reason"] = None

    input_uids = {str(x.get("evidence_uid")) for x in evidence if x.get("evidence_uid")}
    if len(input_uids) != len(evidence):
        errors.append("summarizer_input_missing_uid")

    by_uid: dict[str, dict[str, Any]] = {}
    duplicate_uids: set[str] = set()
    seen_output_uids: set[str] = set()
    for index, output_item in enumerate(items or []):
        if not isinstance(output_item, dict):
            errors.append(f"summarizer_item_not_object:{index}")
            continue
        uid_raw = output_item.get("evidence_uid")
        if not uid_raw:
            errors.append(f"summarizer_item_missing_uid:{index}")
            continue
        uid = str(uid_raw)
        if uid in seen_output_uids:
            duplicate_uids.add(uid)
            continue
        seen_output_uids.add(uid)
        by_uid[uid] = output_item

    if duplicate_uids:
        errors.append("summarizer_duplicate_uids:" + ",".join(sorted(duplicate_uids)))
        for uid in duplicate_uids:
            by_uid.pop(uid, None)
            for item in evidence:
                if str(item.get("evidence_uid") or "") == uid:
                    item["summary_isolation_reason"] = "duplicate_output_uid"

    unknown_uids = sorted(uid for uid in seen_output_uids if uid not in input_uids)
    if unknown_uids:
        errors.append("summarizer_unknown_uids:" + ",".join(unknown_uids))
        for uid in unknown_uids:
            by_uid.pop(uid, None)

    missing_uids = sorted(uid for uid in input_uids if uid not in seen_output_uids)
    if missing_uids:
        errors.append("summarizer_missing_uids:" + ",".join(missing_uids))

    for item in evidence:
        uid = str(item.get("evidence_uid") or "")
        summary = by_uid.get(uid)
        if summary is None:
            item["accept"] = False
            if not item.get("summary_isolation_reason"):
                item["summary_isolation_reason"] = "missing_output"
            continue
        try:
            validate_summary_identity(item, summary)
        except AssertionError as exc:
            item["accept"] = False
            item["summary_isolation_reason"] = "identity_mismatch"
            item["summary_identity_error"] = str(exc)
            errors.append(f"summary_identity_mismatch:{uid}:{exc}")
            continue

        if "accept" not in summary or not isinstance(summary.get("accept"), bool):
            item["accept"] = False
            item["summary_isolation_reason"] = "missing_or_invalid_accept"
            errors.append(f"summarizer_missing_accept:{uid}")
            continue

        # This particular mapping is structurally safe.  It may still be an
        # explicit rejection (accept=false), but it can no longer contaminate a
        # different Evidence item.
        item["summary_integrity_ok"] = True
        item["summary_isolation_reason"] = None
        item["accept"] = summary.get("accept") is True
        item["reject_reason"] = summary.get("reject_reason")
        if not item["accept"]:
            item["decision_rejected"] = True
        item["event_title_zh"] = str(summary.get("event_title_zh") or item.get("title") or "")
        item["what_happened_zh"] = str(summary.get("what_happened_zh") or "")
        item["what_changed_zh"] = str(summary.get("what_changed_zh") or "")
        item["why_it_matters_to_ticker_zh"] = str(summary.get("why_it_matters_to_ticker_zh") or "")
        item["portfolio_impact_zh"] = str(summary.get("portfolio_impact_zh") or "")
        item["supports_action"] = str(summary.get("supports_action") or "none")
        item["does_not_prove_zh"] = str(summary.get("does_not_prove_zh") or "")
        item["impact_direction"] = str(summary.get("impact_direction") or "unknown")
        item["impact_horizon"] = str(summary.get("impact_horizon") or "medium")
        item["facts_zh"] = [str(summary.get("what_happened_zh") or "")]
        item["summary_zh"] = str(summary.get("event_title_zh") or item.get("summary_zh") or "")
        item["relevance_reason"] = str(summary.get("why_it_matters_to_ticker_zh") or "")
        try:
            item["summary_confidence"] = max(0.0, min(1.0, float(summary.get("confidence"))))
        except (TypeError, ValueError):
            item["summary_confidence"] = None
        if not item.get("article_fetch_ok") and item.get("summary_confidence") is not None:
            item["summary_confidence"] = min(0.60, item["summary_confidence"])
        item["summary_method"] = "llm_decision_summarizer"
    return errors


def _build_isolation_diagnostics(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Return report-safe per-Evidence isolation diagnostics.

    The full model output is intentionally not copied into the public report.  The
    UID, source and reason are enough to identify whether failures come from
    missing rows, identity mutation or an invalid ``accept`` field.
    """
    reasons: dict[str, int] = {}
    by_ticker: dict[str, int] = {}
    isolated_items: list[dict[str, Any]] = []
    for item in evidence or []:
        reason = str(item.get("summary_isolation_reason") or "").strip()
        if not reason:
            continue
        ticker = str(item.get("ticker") or "MACRO")
        reasons[reason] = reasons.get(reason, 0) + 1
        by_ticker[ticker] = by_ticker.get(ticker, 0) + 1
        isolated_items.append({
            "evidence_uid": item.get("evidence_uid"),
            "ticker": item.get("ticker"),
            "title": item.get("title") or item.get("raw_title"),
            "source_domain": item.get("source_domain"),
            "source_type": item.get("source_type") or "unknown",
            "lane": item.get("lane") or "unknown",
            "reason": reason,
            "identity_error": item.get("summary_identity_error"),
        })
    return {
        "isolated_count": len(isolated_items),
        "isolation_reasons": reasons,
        "isolated_by_ticker": by_ticker,
        "isolated_items": isolated_items,
    }
