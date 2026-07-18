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
    """Summarize all selected evidence in one lightweight LLM call.

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

    payload = []
    for item in evidence:
        ticker = item.get("ticker")
        payload.append({
            "evidence_id": item.get("evidence_id"),
            "ticker": ticker,
            "instrument": instrument_metadata.get(ticker, {}) if ticker else {},
            "title": item.get("title"),
            "snippet": item.get("summary"),
            "article_facts": item.get("facts") or [],
            "source": item.get("source_name"),
            "date": item.get("published_date"),
            "content_basis": item.get("content_basis"),
            "article_fetch_ok": bool(item.get("article_fetch_ok")),
        })
    prompt = (
        "你是证据摘要器，只能基于输入事实做忠实中文摘要，不得生成投资或交易建议，不得补充输入中没有的事实。"
        "article_fetch_ok=false 表示内容仅来自搜索结果摘要，必须按未验证线索表述，不得改写成已确认事实，也不得补充因果关系。"
        "返回严格 JSON：{\"items\":[{\"evidence_id\":\"E001\",\"facts_zh\":[\"...\"],"
        "\"summary_zh\":\"...\",\"impact_direction\":\"positive|negative|mixed|neutral|unknown\","
        "\"impact_horizon\":\"short|medium|long\",\"relevance_reason\":\"...\",\"confidence\":0.0}]}。\n"
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
        by_id = {str(x.get("evidence_id")): x for x in parsed.get("items", []) if isinstance(x, dict)}
        for item in evidence:
            summary = by_id.get(str(item.get("evidence_id")))
            if not summary:
                continue
            item["facts_zh"] = [str(x) for x in summary.get("facts_zh") or []]
            item["summary_zh"] = str(summary.get("summary_zh") or item.get("summary_zh") or "")
            item["impact_direction"] = str(summary.get("impact_direction") or "unknown")
            item["impact_horizon"] = str(summary.get("impact_horizon") or "medium")
            item["relevance_reason"] = str(summary.get("relevance_reason") or "")
            try:
                item["summary_confidence"] = max(0.0, min(1.0, float(summary.get("confidence"))))
            except (TypeError, ValueError):
                item["summary_confidence"] = None
            if not item.get("article_fetch_ok") and item.get("summary_confidence") is not None:
                item["summary_confidence"] = min(0.60, item["summary_confidence"])
            item["summary_method"] = "llm_evidence_summarizer"
        covered = sum(1 for item in evidence if item.get("summary_method") == "llm_evidence_summarizer")
        return {
            "status": "success" if covered == len(evidence) else "partial",
            "evidence": evidence,
            "summarized_count": covered,
            "errors": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "provider_error", "evidence": evidence, "errors": [f"{type(exc).__name__}: {exc}"]}
