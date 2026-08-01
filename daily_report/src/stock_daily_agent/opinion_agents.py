"""Bounded, tool-free specialist opinions for the decision dashboard.

These are deliberately not autonomous research agents.  Each call receives
only the already-built local decision context, returns a small typed opinion,
and cannot calculate a score, propose levels, browse, or render HTML.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
import json
import os
import time
from typing import Any

from .config import build_llm_cfg
from .decision_schema import OpinionAgentName, OpinionAgentOutput
from .decision_synthesizer import _extract_content, _parse_json, _resolve_model, _resolve_provider


_AGENTS: tuple[OpinionAgentName, ...] = ("technical", "news_fundamental", "risk")
_TECHNICAL_IDS = {"TECH-001", "TECH-002", "TECH-003"}


@dataclass(frozen=True)
class OpinionSynthesisResult:
    opinions: list[OpinionAgentOutput]
    errors: dict[str, str]
    provider: str
    model: str
    elapsed_seconds: float


def opinion_agents_enabled() -> bool:
    return os.environ.get("DECISION_OPINION_AGENTS_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def _allowed_ids(context: dict[str, Any], agent: OpinionAgentName) -> set[str]:
    all_ids = {str(value).strip().upper() for value in context.get("allowed_evidence_ids") or [] if str(value).strip()}
    if agent == "technical":
        return all_ids & _TECHNICAL_IDS
    if agent == "news_fundamental":
        return all_ids - _TECHNICAL_IDS
    return all_ids


def _has_chinese_narrative(value: object) -> bool:
    text = str(value or "").strip()
    return not any("A" <= char <= "z" for char in text) or any("\u4e00" <= char <= "\u9fff" for char in text)


def _bound_evidence_ids(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the schema's deterministic maximum before validating LLM JSON.

    A specialist that cites six otherwise-valid sources should not lose its
    entire opinion merely because the presentation contract permits five.
    Invalid IDs remain rejected later; this helper only truncates an oversized
    list without manufacturing or replacing citations.
    """
    evidence_ids = payload.get("evidence_ids")
    if not isinstance(evidence_ids, list) or len(evidence_ids) <= 5:
        return payload
    bounded = dict(payload)
    bounded["evidence_ids"] = evidence_ids[:5]
    return bounded


def _agent_context(context: dict[str, Any], agent: OpinionAgentName) -> dict[str, Any]:
    allowed = _allowed_ids(context, agent)
    evidence = [
        item for item in context.get("evidence_items") or []
        if str(item.get("evidence_id") or "").upper() in allowed
    ]
    payload: dict[str, Any] = {
        "instrument": context.get("instrument") or {},
        "market_session": context.get("market_session") or {},
        "authoritative_rating": context.get("authoritative_rating") or {},
        "data_quality": context.get("data_quality") or {},
        "allowed_evidence_ids": sorted(allowed),
    }
    if agent in {"technical", "risk"}:
        payload["technical"] = context.get("technical") or {}
        payload["level_candidates"] = context.get("level_candidates") or {}
    if agent in {"news_fundamental", "risk"}:
        payload["evidence_items"] = evidence
    return payload


def _prompt(context: dict[str, Any], agent: OpinionAgentName) -> str:
    payload = json.dumps(_agent_context(context, agent), ensure_ascii=False, separators=(",", ":"))
    return (
        "你是名称为 '" + agent + "' 的受限专业意见组件。只输出 JSON，且字段只能是：agent、signal (buy|hold|sell)、"
        "confidence (0..1)、reason、evidence_ids。reason 必须使用简体中文（ticker、证据 ID、价格可保留原样）。"
        "agent 必须为 '" + agent + "'。不得调用工具、联网、计算/改写评分、编造价格点位、给出最终组合动作或生成 HTML。"
        "只能使用本地 Context 和 allowed_evidence_ids；无支持证据时 evidence_ids 可为空。\n"
        "LOCAL CONTEXT:\n" + payload
    )


def _call_opinion(agent: OpinionAgentName, context: dict[str, Any], cfg: dict[str, Any]) -> OpinionAgentOutput:
    from qwen_agent.llm import get_chat_model

    llm = get_chat_model(cfg)
    # No functions/tools argument: the specialist cannot enter an Agent loop.
    final: Any = []
    for final in llm.chat(
        messages=[
            {"role": "system", "content": "Return one strict JSON object only. You have no tools."},
            {"role": "user", "content": _prompt(context, agent)},
        ],
        stream=True,
    ):
        pass
    opinion = OpinionAgentOutput.model_validate(
        _bound_evidence_ids(_parse_json(_extract_content(final)))
    )
    if opinion.agent != agent:
        raise ValueError("opinion agent identity mismatch")
    if not _has_chinese_narrative(opinion.reason):
        raise ValueError("opinion reason must be Chinese")
    allowed = _allowed_ids(context, agent)
    invalid = [value for value in opinion.evidence_ids if str(value).strip().upper() not in allowed]
    if invalid:
        raise ValueError("opinion contains invalid evidence IDs")
    return opinion


def synthesize_opinions(
    context: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> OpinionSynthesisResult:
    """Run up to three bounded calls concurrently; failures remain non-fatal."""
    started = time.perf_counter()
    selected_provider = ""
    selected_model = ""
    errors: dict[str, str] = {}
    opinions: list[OpinionAgentOutput] = []
    try:
        selected_provider = _resolve_provider(provider or os.environ.get("DECISION_OPINION_AGENTS_PROVIDER"))
        selected_model = _resolve_model(selected_provider, model or os.environ.get("DECISION_OPINION_AGENTS_MODEL"))
        cfg = build_llm_cfg(selected_model, selected_provider)
        generate_cfg = dict(cfg.get("generate_cfg") or {})
        generate_cfg.update({"temperature": 0.0, "max_retries": 0, "max_input_tokens": 6000})
        cfg["generate_cfg"] = generate_cfg
        timeout = max(1, int(os.environ.get("DECISION_OPINION_AGENTS_TIMEOUT_SECONDS", "45")))
        executor = ThreadPoolExecutor(max_workers=len(_AGENTS), thread_name_prefix="decision-opinion")
        futures = {executor.submit(_call_opinion, agent, context, dict(cfg)): agent for agent in _AGENTS}
        done, pending = wait(futures, timeout=timeout)
        for future in pending:
            errors[futures[future]] = f"timed out after {timeout} seconds"
            future.cancel()
        for future in done:
            agent = futures[future]
            try:
                opinions.append(future.result())
            except Exception as exc:
                errors[agent] = str(exc)[:500]
        executor.shutdown(wait=not pending, cancel_futures=True)
    except Exception as exc:
        errors["setup"] = str(exc)[:500]
    order = {agent: index for index, agent in enumerate(_AGENTS)}
    opinions.sort(key=lambda item: order[item.agent])
    return OpinionSynthesisResult(opinions, errors, selected_provider, selected_model, time.perf_counter() - started)


def summarize_opinion_conflict(opinions: list[OpinionAgentOutput]) -> str | None:
    """Return a deterministic explanation; specialist prose never owns this conclusion."""
    if len(opinions) < 2:
        return "专业意见未完整返回；程序保留既有的保守风险护栏。"
    signals = {opinion.signal for opinion in opinions}
    if len(signals) == 1:
        return "可用专业意见方向一致；程序评分与风险护栏仍为最终依据。"
    labels = "、".join({"technical": "技术", "news_fundamental": "消息/基本面", "risk": "风险"}.get(item.agent, item.agent) + "=" + {"buy": "买入", "hold": "持有", "sell": "卖出"}.get(item.signal, item.signal) for item in opinions)
    return f"专业意见存在分歧（{labels}）；程序保留权威评分并应用保守风险护栏。"
