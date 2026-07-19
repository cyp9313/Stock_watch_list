# -*- coding: utf-8 -*-
"""Portfolio AI Agent 运行器。

真正调用 Qwen-Agent Assistant（复用项目 build_llm_cfg），让模型通过
Portfolio tools 读取确定性数据并写出结构化建议。校验失败时让模型修复一次；
若 LLM 未配置或调用失败，抛出 PortfolioAgentUnavailable，由上层生成被明确
标记的量化降级报告（修改计划 5.2 方案 B）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from .config import build_llm_cfg
from .portfolio_context import PortfolioRunContext
from .portfolio_prompts import build_portfolio_system_prompt, build_portfolio_user_task
from .portfolio_schema import normalize_advice
from .portfolio_tools import build_portfolio_tools, set_portfolio_context
from portfolio_analysis.action_targets import apply_deterministic_action_targets
from portfolio_analysis.validators import (
    validate_portfolio_advice, PortfolioAdviceValidationError, validate_portfolio_claims,
)


class PortfolioAgentUnavailable(RuntimeError):
    """LLM 未配置或不可用；应生成量化降级报告而非伪 AI 报告。"""


class PortfolioAgentOutputError(RuntimeError):
    """Agent 未产出可校验的建议。"""


_FRESH_EVIDENCE_TIERS = {"fresh_event", "recent_background"}

_DISPLAY_TERM_REPLACEMENTS = {
    "search_snippet_unverified": "搜索摘要",
    "article_body_verified": "正文已提取",
    "recent_background": "近期背景信息",
    "fresh_event": "新鲜事件",
    "content_basis=": "内容依据：",
    "rsi_regime": "RSI 区间",
    "overbought": "超买",
    "oversold": "超卖",
    "neutral": "中性",
    "strong": "偏强",
    "weak": "偏弱",
    "evidence_count": "证据数量",
    "portfolio_risk_score": "风险评分",
    "cash_unspecified": "现金（未指定）",
    "uranium_price": "铀价",
    # P1 §30: 枚举中文化
    "medium": "中等",
    "actual\":": "数据有效\":",
    "'actual'": "'数据有效'",
    "earnings_results": "财报结果",
    "earnings_preview": "财报前瞻",
    "general_event": "一般事件",
    "unknown": "未知",
    "short": "短期",
    "mixed": "影响混合",
    "add": "加仓候选",
}


def _display_text(value: Any) -> str:
    text = str(value or "")
    for source, replacement in _DISPLAY_TERM_REPLACEMENTS.items():
        pattern = (
            re.escape(source)
            if "_" in source or "=" in source
            else rf"(?<![A-Za-z0-9_]){re.escape(source)}(?![A-Za-z0-9_])"
        )
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def _sanitize_user_facing_language(advice: dict[str, Any]) -> dict[str, Any]:
    """Translate internal enums in human-facing prose without touching machine fields."""
    advice["executive_summary"] = [_display_text(item) for item in advice.get("executive_summary") or []]
    portfolio_analysis = advice.get("portfolio_analysis") or {}
    for key, value in list(portfolio_analysis.items()):
        if isinstance(value, str):
            portfolio_analysis[key] = _display_text(value)
    for risk in advice.get("key_risks") or []:
        if isinstance(risk, dict):
            for key in ("title", "description"):
                risk[key] = _display_text(risk.get(key))
    for action in advice.get("actions") or []:
        if not isinstance(action, dict):
            continue
        for key in ("action_zh", "portfolio_reason", "technical_reason", "news_reason", "bull_case", "bear_case"):
            if action.get(key) is not None:
                action[key] = _display_text(action.get(key))
        for key in ("execute_if", "cancel_or_upgrade_if", "further_reduce_if", "monitoring_items"):
            action[key] = [_display_text(item) for item in action.get(key) or []]
        for threshold in action.get("thresholds") or []:
            if isinstance(threshold, dict):
                threshold["note"] = _display_text(threshold.get("note"))
    for item in advice.get("watch_items") or []:
        if isinstance(item, dict):
            item["title"] = _display_text(item.get("title"))
            item["reason"] = _display_text(item.get("reason"))
    advice["data_limitations"] = [_display_text(item) for item in advice.get("data_limitations") or []]
    advice["disclaimer"] = _display_text(advice.get("disclaimer"))
    return advice


def _remove_sentences_citing(text: Any, evidence_id: str) -> str:
    value = str(text or "")
    if not re.search(rf"(?<![A-Za-z0-9]){re.escape(evidence_id)}(?![A-Za-z0-9])", value, re.I):
        return value
    sentences = re.split(r"(?<=[。！？.!?])\s*", value)
    kept = [
        sentence for sentence in sentences
        if sentence and not re.search(
            rf"(?<![A-Za-z0-9]){re.escape(evidence_id)}(?![A-Za-z0-9])", sentence, re.I,
        )
    ]
    return "".join(kept).strip()


def _sanitize_evidence_references(advice: dict[str, Any], ctx: PortfolioRunContext) -> dict[str, Any]:
    """Drop unknown/cross-ticker evidence IDs before strict semantic validation.

    Removing an invalid citation is safer than letting it support a claim.  The
    removal is recorded and confidence controls run afterwards using only the
    citations that remain.
    """
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in ctx.evidence
        if item.get("evidence_id")
    }
    warnings = advice.setdefault("validation_warnings", [])
    for action in advice.get("actions") or []:
        if not isinstance(action, dict):
            continue
        ticker = str(action.get("ticker") or "").upper()
        valid_ids: list[str] = []
        for raw_id in action.get("evidence_ids") or []:
            evidence_id = str(raw_id)
            item = evidence_by_id.get(evidence_id)
            related = {str(value).upper() for value in (item or {}).get("related_tickers") or []}
            owner = str((item or {}).get("ticker") or "").upper()
            if item is not None and (owner == ticker or ticker in related):
                valid_ids.append(evidence_id)
            else:
                warnings.append(f"已移除 {ticker} 操作中的无效或跨标的证据引用 {evidence_id}。")
                for key in ("portfolio_reason", "technical_reason", "news_reason", "bull_case", "bear_case"):
                    action[key] = _remove_sentences_citing(action.get(key), evidence_id)
        action["evidence_ids"] = list(dict.fromkeys(valid_ids))
        if not action["evidence_ids"] and not str(action.get("news_reason") or "").strip():
            action["news_reason"] = "没有与该标的匹配的可用新闻证据，本操作不采用新闻结论。"

    for risk in advice.get("key_risks") or []:
        if not isinstance(risk, dict):
            continue
        affected = {str(value).upper() for value in risk.get("affected_tickers") or []}
        valid_ids = []
        for raw_id in risk.get("evidence_ids") or []:
            evidence_id = str(raw_id)
            item = evidence_by_id.get(evidence_id)
            related = {str(value).upper() for value in (item or {}).get("related_tickers") or []}
            owner = str((item or {}).get("ticker") or "").upper()
            if item is not None and ((owner and owner in affected) or bool(affected & related)):
                valid_ids.append(evidence_id)
            else:
                warnings.append(
                    f"已移除关键风险 {risk.get('risk_id') or '未编号'} 中的无效或跨标的证据引用 {evidence_id}。"
                )
                risk["description"] = _remove_sentences_citing(risk.get("description"), evidence_id)
        risk["evidence_ids"] = list(dict.fromkeys(valid_ids))
        if not str(risk.get("description") or "").strip():
            risk["description"] = "该风险仅保留确定性指标依据；不匹配的新闻证据引用已移除。"
    return advice


def _apply_python_owned_action_controls(advice: dict[str, Any], ctx: PortfolioRunContext) -> dict[str, Any]:
    """Apply deterministic targets and evidence-based confidence caps before validation.

    Target weights and final confidence are Python-owned fields.  Applying these controls
    before the Agent validation pass prevents temporary LLM values from failing rules that
    the pipeline would overwrite immediately afterwards.
    """
    advice = apply_deterministic_action_targets(advice, ctx.metrics, ctx.settings)
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in ctx.evidence
        if item.get("evidence_id")
    }
    fresh_tickers = {
        str(item.get("ticker") or "").upper()
        for item in ctx.evidence
        if item.get("ticker") and item.get("recency_tier") in _FRESH_EVIDENCE_TIERS
    }
    verified_fresh_tickers = {
        str(item.get("ticker") or "").upper()
        for item in ctx.evidence
        if item.get("ticker")
        and item.get("recency_tier") in _FRESH_EVIDENCE_TIERS
        and item.get("article_fetch_ok")
    }
    for action in advice.get("actions") or []:
        ticker = str(action.get("ticker") or "").upper()
        try:
            confidence = max(0.0, min(1.0, float(action.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        referenced_verified_fresh = any(
            str((evidence_by_id.get(str(evidence_id)) or {}).get("recency_tier")) in _FRESH_EVIDENCE_TIERS
            and bool((evidence_by_id.get(str(evidence_id)) or {}).get("article_fetch_ok"))
            for evidence_id in action.get("evidence_ids") or []
        )
        if confidence >= 0.7 and not referenced_verified_fresh:
            action["model_confidence"] = round(confidence, 3)
            if ticker in verified_fresh_tickers:
                action["confidence"] = 0.69
            elif ticker in fresh_tickers:
                action["confidence"] = 0.60
            else:
                action["confidence"] = 0.30
            action["confidence_note"] = "未引用该标的新鲜且正文已提取的证据，操作置信度已由 Python 限制。"
    return advice


def _validate_agent_advice(advice: dict[str, Any], ctx: PortfolioRunContext) -> dict[str, Any]:
    normalized = normalize_advice(
        advice,
        snapshot=ctx.snapshot,
        metrics=ctx.metrics,
        ranking=ctx.ranking,
    )
    normalized = _sanitize_user_facing_language(normalized)
    normalized = _sanitize_evidence_references(normalized, ctx)
    normalized = _apply_python_owned_action_controls(normalized, ctx)
    # Pre-guard: 无 accepted evidence 时 exit→reduce，避免 strict 模式硬报错
    for action in normalized.get("actions") or []:
        if str(action.get("action") or "").lower() == "exit" and not (action.get("evidence_ids") or []):
            action["action"] = "reduce"
    validated = validate_portfolio_advice(normalized, ctx.snapshot, ctx.evidence, mode="strict")
    claim_errors, claim_warnings = validate_portfolio_claims(
        validated, ctx.snapshot, ctx.metrics, ctx.evidence,
    )
    if claim_warnings:
        validated.setdefault("validation_warnings", []).extend(claim_warnings)
    if claim_errors:
        raise PortfolioAdviceValidationError(claim_errors)
    return validated


def _require_qwen_agent():
    try:
        from qwen_agent.agents import Assistant
    except Exception as exc:  # noqa: BLE001
        raise PortfolioAgentUnavailable("qwen-agent 未安装。") from exc

    class JsonSafeAssistant(Assistant):
        def _call_llm(self, *args, **kwargs):
            final_output = []
            for output in super()._call_llm(*args, **kwargs):
                final_output = output
            if final_output:
                _normalize_function_arguments(final_output)
                yield final_output

    return JsonSafeAssistant


def _normalize_function_arguments(messages: list[Any]) -> None:
    for message in messages:
        function_call = getattr(message, "function_call", None)
        if function_call and isinstance(getattr(function_call, "arguments", None), str):
            function_call.arguments = _strict_json(function_call.arguments)


def _strict_json(text: str) -> str:
    text = (text or "").strip()
    candidates = [text]
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) > 2:
            candidates.append("\n".join(lines[1:-1]).strip())
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False)
    return json.dumps({"_invalid": text[:1000]}, ensure_ascii=False)


def _plain_text_from_response(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, list):
        return _plain_text_from_response(response[-1]) if response else ""
    if isinstance(response, dict):
        content = response.get("content", "")
    else:
        content = getattr(response, "content", str(response))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or p.get("content") or "") for p in content)
    return str(content)


def _extract_advice_from_text(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    chunk = text[start:end + 1]
    for loader in (json.loads,):
        try:
            parsed = loader(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and ("actions" in parsed or "portfolio_stance" in parsed or "executive_summary" in parsed):
            return parsed
    return None


def _llm_configured(provider: str) -> bool:
    provider = (provider or "dashscope").lower()
    if provider == "dashscope":
        return bool(os.environ.get("DASHSCOPE_API_KEY"))
    if provider == "deepseek":
        return bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    if provider == "openai_compatible":
        return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("QWEN_API_KEY"))
    return False


def run_portfolio_agent(
    ctx: PortfolioRunContext,
    model: str,
    provider: str = "dashscope",
    *,
    verbose: bool = True,
) -> dict[str, Any]:
    if not _llm_configured(provider):
        raise PortfolioAgentUnavailable(f"未配置 {provider} 的 LLM API Key，无法运行 Portfolio AI Agent。")

    Assistant = _require_qwen_agent()
    set_portfolio_context(ctx)
    llm_cfg = build_llm_cfg(model=model, provider=provider)
    system_message = build_portfolio_system_prompt()
    tools = build_portfolio_tools()

    bot = Assistant(
        llm=llm_cfg,
        system_message=system_message,
        function_list=tools,
    )
    user_task = build_portfolio_user_task(ctx)
    messages: list[dict] = [{"role": "user", "content": user_task}]

    last_text = ""
    for response in bot.run(messages=messages):
        text = _plain_text_from_response(response)
        if verbose and text:
            # 避免重复打印
            delta = text[len(last_text):] if text.startswith(last_text) else text
            print(delta, end="", flush=True)
            last_text = text
    if verbose and last_text and not last_text.endswith("\n"):
        print()

    advice = getattr(ctx, "_saved_advice", None)
    if advice is None:
        advice = _extract_advice_from_text(last_text)
    if advice is None:
        raise PortfolioAgentOutputError("Agent 未返回可解析的建议 JSON。")

    try:
        return _validate_agent_advice(advice, ctx)
    except PortfolioAdviceValidationError as exc:
        retry_msg = (
            f"你的建议校验未通过，请修正后再次调用 save_portfolio_advice：\n"
            + "\n".join(f"- {e}" for e in exc.errors)
            + "\n修复规则：没有为该 ticker 引用 fresh_event/recent_background 证据时，"
              "action confidence 必须低于 0.70；目标仓位由 Python 计算，不要据此改变 action。"
              "只能引用属于 affected_tickers/action ticker 的 evidence_id；没有匹配证据时使用空列表，"
              "不要在中文正文中输出 weak/neutral/strong/oversold/overbought 等内部枚举。"
        )
        messages.append({"role": "user", "content": retry_msg})
        ctx._saved_advice = None
        last_text = ""
        for response in bot.run(messages=messages):
            text = _plain_text_from_response(response)
            if verbose and text:
                delta = text[len(last_text):] if text.startswith(last_text) else text
                print(delta, end="", flush=True)
                last_text = text
        advice = getattr(ctx, "_saved_advice", None)
        if advice is None:
            advice = _extract_advice_from_text(last_text)
        if advice is None:
            raise PortfolioAgentOutputError("重试后 Agent 仍未返回可校验的建议。")
        try:
            return _validate_agent_advice(advice, ctx)
        except PortfolioAdviceValidationError as retry_exc:
            raise PortfolioAgentOutputError(
                "Portfolio Agent validation failed: " + "; ".join(retry_exc.errors)
            ) from retry_exc
