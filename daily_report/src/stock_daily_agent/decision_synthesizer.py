"""One-shot, tool-free LLM synthesis for the decision dashboard."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
import json
import os
import re
import time
from typing import Any

from .config import build_llm_cfg
from .decision_schema import DecisionDashboard


@dataclass(frozen=True)
class DecisionSynthesisResult:
    ok: bool
    proposal: DecisionDashboard | None
    raw_text: str
    provider: str
    model: str
    error: str | None
    elapsed_seconds: float


def decision_synthesis_enabled() -> bool:
    return os.environ.get("DECISION_REPORT_ENABLED", "true").strip().lower() not in {"0", "false", "no"}


def _resolve_provider(provider: str | None) -> str:
    value = (provider or os.environ.get("DECISION_REPORT_PROVIDER") or "inherit").strip().lower()
    if value == "inherit":
        value = (os.environ.get("LLM_PROVIDER") or os.environ.get("MODEL_PROVIDER") or "auto").strip().lower()
    if value == "auto":
        key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        value = "deepseek" if key and not key.lower().startswith("your_") else "dashscope"
    if value not in {"dashscope", "deepseek", "openai_compatible"}:
        raise ValueError(f"Unsupported decision report provider: {value}")
    return value


def _resolve_model(provider: str, model: str | None) -> str:
    explicit = (model or os.environ.get("DECISION_REPORT_MODEL") or "").strip()
    if explicit:
        return explicit
    if provider == "deepseek":
        return (os.environ.get("LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash").strip()
    return (os.environ.get("LLM_MODEL") or os.environ.get("QWEN_MODEL") or "qwen-plus").strip()


def _extract_content(messages: Any) -> str:
    if isinstance(messages, list) and messages:
        message = messages[-1]
    else:
        message = messages
    if isinstance(message, dict):
        content = message.get("content", "")
    else:
        content = getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(str(part.get("text") or part.get("content") or "") if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


def _parse_json(text: str) -> dict[str, Any]:
    candidates = [text.strip()]
    fenced = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.IGNORECASE)
    candidates.append(fenced.strip())
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first:last + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Decision model did not return a valid JSON object")


def _prompt(context: dict[str, Any]) -> str:
    schema = json.dumps(DecisionDashboard.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    payload = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    return (
        "You produce a controlled investment decision dashboard. Output JSON only, matching the supplied JSON schema exactly. "
        "Do not call tools, browse, add fields, calculate a score, invent an evidence ID, invent a price, guarantee returns, "
        "or contradict market_session. final_score must exactly copy authoritative_rating.final_score. "
        "Use only allowed_evidence_ids. Any levels field must copy a non-null string from level_candidates.display or be null. "
        "Give distinct no_position and has_position advice. If evidence is limited, prefer watch or hold. "
        "The final action may later be changed by Python guardrails.\nSCHEMA:\n"
        + schema + "\nCONTEXT:\n" + payload
    )


def _call_model(messages: list[dict[str, str]], cfg: dict[str, Any]) -> str:
    from qwen_agent.llm import get_chat_model

    llm = get_chat_model(cfg)
    # No functions argument: this call cannot enter the Agent tool loop.
    stream = llm.chat(messages=messages, stream=True)
    final: Any = []
    for final in stream:
        pass
    return _extract_content(final)


def synthesize_decision(
    context: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> DecisionSynthesisResult:
    """Make at most one no-tool model call; callers fall back on any failure."""
    started = time.perf_counter()
    selected_provider = ""
    selected_model = ""
    try:
        selected_provider = _resolve_provider(provider)
        selected_model = _resolve_model(selected_provider, model)
        cfg = build_llm_cfg(selected_model, selected_provider)
        generate_cfg = dict(cfg.get("generate_cfg") or {})
        generate_cfg.update({"temperature": float(os.environ.get("DECISION_REPORT_TEMPERATURE", "0.1")), "max_retries": 0, "max_input_tokens": 12000})
        cfg["generate_cfg"] = generate_cfg
        timeout = max(1, int(os.environ.get("DECISION_REPORT_TIMEOUT_SECONDS", "120")))
        messages = [
            {"role": "system", "content": "Return only a strict JSON object. You are not an autonomous agent."},
            {"role": "user", "content": _prompt(context)},
        ]
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="decision-synthesis")
        future = executor.submit(_call_model, messages, cfg)
        try:
            raw_text = future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            future.cancel()
            # Do not wait for an unhealthy provider request; report generation
            # must proceed immediately to its deterministic fallback.
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(f"Decision synthesis timed out after {timeout} seconds") from exc
        executor.shutdown(wait=True)
        proposal = DecisionDashboard.model_validate(_parse_json(raw_text))
        return DecisionSynthesisResult(True, proposal, raw_text, selected_provider, selected_model, None, time.perf_counter() - started)
    except Exception as exc:
        return DecisionSynthesisResult(False, None, "", selected_provider, selected_model, str(exc), time.perf_counter() - started)
