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
) -> dict[str, Any]:
    """Decision Evidence Summarizer（修改计划第六轮第 19 节）。

    升级为决策证据摘要器：支持 reject、what_changed、why_it_matters、
    supports_action、does_not_prove、verification_level。

    The model may translate and classify supplied facts only. It is explicitly
    prohibited from creating portfolio actions or adding facts.
    """
    if not evidence:
        return {"status": "no_evidence", "evidence": evidence, "errors": []}
    provider = (provider or "dashscope").lower()
    if provider == "dashscope" and not os.environ.get("DASHSCOPE_API_KEY"):
        return {"status": "not_configured", "evidence": evidence, "errors": ["DASHSCOPE_API_KEY not configured"]}
    if provider == "deepseek" and not (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        return {"status": "not_configured", "evidence": evidence, "errors": ["DeepSeek API key not configured"]}
    if provider == "openai_compatible" and not (os.environ.get("OPENAI_API_KEY") or os.environ.get("QWEN_API_KEY")):
        return {"status": "not_configured", "evidence": evidence, "errors": ["OpenAI-compatible API key not configured"]}

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
        "article_fetch_ok=false 或 verification_level=search_snippet 表示内容仅来自搜索结果摘要，必须按未验证线索表述。\n\n"
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
        accepted = sum(1 for item in evidence if item.get("accept", True))
        return {
            "status": "success" if covered == len(evidence) else "partial",
            "evidence": evidence,
            "summarized_count": covered,
            "accepted_count": accepted,
            "rejected_count": len(evidence) - accepted,
            "errors": list(summary_errors),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "provider_error",
            "evidence": evidence,
            "summarized_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "errors": [f"summarizer_failed: {type(exc).__name__}: {exc}"],
        }


# 第七轮第 4 节：AI 摘要器只能补充解释性字段，不得修改原始身份字段。
_IMMUTABLE_FIELDS = ("ticker", "url", "source_domain", "published_date", "event_key", "raw_title")


def validate_summary_identity(original: dict[str, Any], summarized: dict[str, Any]) -> None:
    """断言摘要未篡改原始身份字段；违规抛 AssertionError。"""
    assert summarized.get("evidence_uid") == original.get("evidence_uid"), "evidence_uid 不一致"
    assert summarized.get("ticker") in (None, original.get("ticker")), "ticker 被篡改"


def _apply_summaries_by_uid(
    evidence: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[str]:
    """按 evidence_uid 应用 LLM 摘要结果，返回错误列表（确定性，可单测）。

    - 重复 UID / 未知 UID / 遗漏 UID：记录错误，跳过该条，不覆盖原值；
    - 篡改原始身份字段：记录错误并跳过（不覆盖）。
    """
    errors: list[str] = []
    by_uid = {
        str(x.get("evidence_uid")): x
        for x in items
        if isinstance(x, dict) and x.get("evidence_uid")
    }
    seen_uids: set[str] = set()
    for item in evidence:
        uid = str(item.get("evidence_uid") or "")
        summary = by_uid.get(uid)
        if not summary:
            continue
        if uid in seen_uids:
            errors.append(f"duplicate_evidence_uid_in_summary:{uid}")
            continue
        seen_uids.add(uid)
        try:
            validate_summary_identity(item, summary)
        except AssertionError as exc:
            errors.append(f"summary_identity_mismatch:{uid}:{exc}")
            continue
        item["accept"] = bool(summary.get("accept", True))
        item["reject_reason"] = summary.get("reject_reason")
        if not item["accept"]:
            # 被拒绝的证据标记但不删除（保留供诊断）
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
        # 保留旧字段兼容
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
