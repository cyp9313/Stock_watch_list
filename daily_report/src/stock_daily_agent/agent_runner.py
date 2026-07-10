from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import os

import json5

from .config import RunContext, build_llm_cfg
from .prompts import build_system_prompt, build_user_task
from .skill_loader import load_skill
from .tools import set_context, build_custom_tools


@dataclass
class AgentRunResult:
    ok: bool
    output_html: Path
    run_dir: Path
    final_messages: list[dict]
    summary_text: str


def _strict_json_arguments(arguments: Any) -> str:
    """Canonicalize model tool arguments before they re-enter API history."""
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False)

    text = str(arguments or "").strip()
    candidates = [text]
    if text.startswith("```"):
        lines = text.splitlines()
        candidates.append("\n".join(lines[1:-1]).strip() if len(lines) > 2 else text.strip("`"))
    first_brace, last_brace = text.find("{"), text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(text[first_brace:last_brace + 1])

    for candidate in candidates:
        if not candidate:
            continue
        for loader in (json.loads, json5.loads):
            try:
                parsed = loader(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False)

    # Keep the conversation valid and let the tool return a useful validation
    # error so the model can retry with corrected arguments.
    return json.dumps({"_invalid_arguments": text[:1000]}, ensure_ascii=False)


def _normalize_function_arguments(messages: list[Any]) -> None:
    for message in messages:
        function_call = getattr(message, "function_call", None)
        if function_call:
            function_call.arguments = _strict_json_arguments(function_call.arguments)


def _require_qwen_agent():
    try:
        from qwen_agent.agents import Assistant
    except Exception as exc:
        raise RuntimeError(
            "qwen-agent 未安装。请先运行: python -m pip install -r requirements.txt"
        ) from exc

    class JsonSafeAssistant(Assistant):
        def _call_llm(self, *args, **kwargs):
            # The raw DashScope stream reuses mutable function-call messages.
            # Buffer the final response before canonicalizing its arguments.
            final_output = []
            for output in super()._call_llm(*args, **kwargs):
                final_output = output
            if final_output:
                _normalize_function_arguments(final_output)
                yield final_output

    return JsonSafeAssistant


def _plain_text_from_response(response: Any) -> str:
    """Best-effort extraction for Qwen-Agent streaming message lists."""
    if response is None:
        return ""
    if isinstance(response, list):
        if not response:
            return ""
        return _plain_text_from_response(response[-1])
    if isinstance(response, dict):
        content = response.get("content", "")
    else:
        content = getattr(response, "content", str(response))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def _search_status(enable_builtin_web: bool) -> tuple[str, bool]:
    searxng_url = os.environ.get("SEARXNG_URL", "").strip()
    serper_available = bool(os.environ.get("SERPER_API_KEY"))
    dashscope_available = bool(os.environ.get("DASHSCOPE_API_KEY"))
    # V5.8: do not enable Qwen-Agent built-in web_search by default even if
    # SERPER_API_KEY exists. Serper should be used via serper_market_research /
    # combined_market_research so results are saved as structured evidence.
    allow_builtin = os.environ.get("ENABLE_BUILTIN_SERPER_WEB_TOOLS", "false").strip().lower() in {"1", "true", "yes"}
    builtin_web_available = bool(enable_builtin_web and allow_builtin and serper_available)

    parts: list[str] = []
    search_provider = os.environ.get("SEARCH_PROVIDER", "both").strip()
    if searxng_url:
        lang = os.environ.get("SEARXNG_LANGUAGE", "auto")
        parts.append(
            f"SearXNG 已配置（SEARXNG_URL={searxng_url}）；SEARCH_PROVIDER={search_provider}。V5.8 默认使用 priority_market_research：Serper 优先、DashScope source fallback、SearXNG 最后兜底；SEARXNG_LANGUAGE={lang}。"
        )
    else:
        parts.append("SearXNG 未配置（缺少 SEARXNG_URL）。")

    if serper_available:
        parts.append("SERPER_API_KEY 已配置；请通过 priority_market_research 或 serper_market_research 使用 Serper，而不是内置 web_search，以保留 evidence_id 审计链。")
    else:
        parts.append("SERPER_API_KEY 未配置；Serper provider 不可用。")

    if builtin_web_available:
        parts.append("ENABLE_BUILTIN_SERPER_WEB_TOOLS=true，因此 Qwen-Agent 内置 web_search/web_extractor 也会加载；仅用于调试，不建议正式报告使用。")
    elif enable_builtin_web:
        parts.append("V5.8 默认跳过 Qwen-Agent 内置 web_search/web_extractor，避免绕过结构化证据链。")
    else:
        parts.append("用户通过 --no-web-tools 禁用了 Qwen-Agent 内置 web_search/web_extractor。")

    if dashscope_available:
        parts.append("DashScope 可用；dashscope_market_research 可作为兜底检索层。")
    else:
        parts.append("DashScope Key 未配置；DashScope 兜底检索可能不可用。")

    return " ".join(parts), builtin_web_available


def build_agent(ctx: RunContext, model: str, provider: str, enable_builtin_web: bool = True):
    Assistant = _require_qwen_agent()
    skill = load_skill(ctx.paths.skill_file)
    status, builtin_web_available = _search_status(enable_builtin_web)
    system_prompt = build_system_prompt(
        skill,
        min_notes=ctx.min_notes,
        search_status=status,
        builtin_web_available=builtin_web_available,
    )
    llm_cfg = build_llm_cfg(model=model, provider=provider)
    tool_mode = "raw/native" if llm_cfg.get("generate_cfg", {}).get("use_raw_api") else "local/parser"
    print(f"[INFO] Main-agent tool-calling mode: {tool_mode}")

    tools: list[Any] = build_custom_tools()
    if builtin_web_available:
        # Qwen-Agent built-in web_search uses Serper and requires SERPER_API_KEY.
        # V5.8 normally keeps this disabled so Serper goes through structured evidence tools.
        tools.extend(["web_search", "web_extractor"])

    if os.environ.get("SEARXNG_URL", "").strip():
        print(
            "[INFO] SEARXNG_URL 已配置：可参与 combined_market_research A/B 测试；"
            "美股/ETF/指数/加密默认英文，港股双语，A股中文。"
        )
    else:
        print(
            "[WARN] SEARXNG_URL 未配置：将跳过 SearXNG，"
            "如无 SERPER_API_KEY 则使用 dashscope_market_research 兜底。"
        )

    if os.environ.get("SERPER_API_KEY"):
        print("[INFO] SERPER_API_KEY 已配置：V5.8 将通过 priority_market_research 优先使用 Serper 结构化结果。")
    elif enable_builtin_web:
        print(
            "[INFO] SERPER_API_KEY 未配置：Serper provider 不可用；不会触发 Serper Key 错误。"
        )

    return Assistant(
        llm=llm_cfg,
        system_message=system_prompt,
        function_list=tools,
    )


def run_agent(
    ctx: RunContext,
    model: str,
    provider: str = "dashscope",
    enable_builtin_web: bool = True,
    verbose: bool = True,
) -> AgentRunResult:
    set_context(ctx)
    bot = build_agent(ctx, model=model, provider=provider, enable_builtin_web=enable_builtin_web)
    user_task = build_user_task(
        ticker=ctx.ticker,
        months=ctx.months,
        output_html=str(ctx.output_html) if ctx.output_html else None,
        report_date=ctx.report_date,
    )
    messages: list[dict] = [{"role": "user", "content": user_task}]

    final_response: Any = None
    last_text = ""
    for response in bot.run(messages=messages):
        final_response = response
        text = _plain_text_from_response(response)
        if verbose and text and text != last_text:
            delta = text[len(last_text):] if text.startswith(last_text) else text
            print(delta, end="", flush=True)
            last_text = text

    if verbose and last_text and not last_text.endswith("\n"):
        print()

    if isinstance(final_response, list):
        final_messages = final_response
    elif final_response is None:
        final_messages = []
    else:
        final_messages = [final_response]

    output = ctx.final_output_html
    return AgentRunResult(
        ok=output.exists() and output.stat().st_size > 0,
        output_html=output,
        run_dir=ctx.run_dir,
        final_messages=final_messages,
        summary_text=_plain_text_from_response(final_response),
    )
