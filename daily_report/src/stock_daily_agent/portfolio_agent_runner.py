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
from typing import Any

from .config import build_llm_cfg
from .portfolio_context import PortfolioRunContext
from .portfolio_prompts import build_portfolio_system_prompt, build_portfolio_user_task
from .portfolio_schema import normalize_advice
from .portfolio_tools import build_portfolio_tools, set_portfolio_context
from portfolio_analysis.validators import (
    validate_portfolio_advice, PortfolioAdviceValidationError, validate_portfolio_claims,
)


class PortfolioAgentUnavailable(RuntimeError):
    """LLM 未配置或不可用；应生成量化降级报告而非伪 AI 报告。"""


class PortfolioAgentOutputError(RuntimeError):
    """Agent 未产出可校验的建议。"""


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

    def _validate(norm: dict) -> dict:
        validated = validate_portfolio_advice(norm, ctx.snapshot, ctx.evidence, mode="strict")
        c_err, c_warn = validate_portfolio_claims(validated, ctx.snapshot, ctx.metrics, ctx.evidence)
        if c_warn:
            validated.setdefault("validation_warnings", []).extend(c_warn)
        if c_err:
            raise PortfolioAdviceValidationError(c_err)
        return validated

    try:
        return _validate(normalize_advice(advice, snapshot=ctx.snapshot, metrics=ctx.metrics, ranking=ctx.ranking))
    except PortfolioAdviceValidationError as exc:
        retry_msg = (
            f"你的建议校验未通过，请修正后再次调用 save_portfolio_advice：\n"
            + "\n".join(f"- {e}" for e in exc.errors)
        )
        messages.append({"role": "user", "content": retry_msg})
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
        return _validate(normalize_advice(advice, snapshot=ctx.snapshot, metrics=ctx.metrics, ranking=ctx.ranking))
