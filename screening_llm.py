"""Bounded, tool-free LLM re-ranking for deterministic screening candidates."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import json
import os
import re
import time
from typing import Any

from daily_report.src.stock_daily_agent.config import build_llm_cfg
from daily_report.src.stock_daily_agent.decision_synthesizer import _extract_content


def _resolve_provider() -> str:
    value = (os.environ.get("SCREENING_RERANK_PROVIDER") or "inherit").strip().lower()
    if value == "inherit":
        value = (os.environ.get("LLM_PROVIDER") or os.environ.get("MODEL_PROVIDER") or "auto").strip().lower()
    if value == "auto":
        key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        value = "deepseek" if key and not key.lower().startswith("your_") else "dashscope"
    if value not in {"dashscope", "deepseek", "openai_compatible"}:
        raise ValueError(f"Unsupported screening rerank provider: {value}")
    return value


def _resolve_model(provider: str) -> str:
    explicit = (os.environ.get("SCREENING_RERANK_MODEL") or "").strip()
    if explicit:
        return explicit
    if provider == "deepseek":
        return (os.environ.get("LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash").strip()
    return (os.environ.get("LLM_MODEL") or os.environ.get("QWEN_MODEL") or "qwen-plus").strip()


def _call_model(messages: list[dict[str, str]], cfg: dict[str, Any]) -> str:
    from qwen_agent.llm import get_chat_model

    llm = get_chat_model(cfg)
    final: Any = []
    for final in llm.chat(messages=messages, stream=True):
        pass
    return _extract_content(final)


def _parse_json(text: str) -> dict[str, Any]:
    candidates = [text.strip(), re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.IGNORECASE).strip()]
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
    raise ValueError("Screening model did not return a valid JSON object")


def _safe_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _build_prompt(strategy: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    rows = []
    for row in candidates:
        rows.append({
            "ticker": row.get("Ticker"), "source": row.get("Source"), "rule_score": row.get("Score"),
            "factor_scores": row.get("Factor Scores"), "risk_tags": row.get("Risk Tags"), "metrics": row.get("Metrics"),
            "rule_reason": row.get("Reason"),
        })
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return (
        "你是股票筛选结果的审慎复核器，不是交易 Agent。仅能重排给定 ticker，不能新增或删除 ticker，"
        "不能修改 rule_score，也不能联网、调用工具、编造新闻或承诺收益。所有面向用户文字必须使用简体中文。\n"
        "仅输出严格 JSON：{\"ranked_tickers\":[按新顺序列出全部 ticker],\"items\":[{\"ticker\":\"...\",\"reason\":\"不超过120字\",\"risk\":\"不超过80字\"}]}。\n"
        f"策略：{_safe_text(strategy.get('label'), 80)}。说明：{_safe_text(strategy.get('description'), 300)}。\n"
        f"候选：{payload}"
    )


def rerank_candidates(strategy: dict[str, Any], limit: int = 15) -> dict[str, Any]:
    """Return a validated re-ordering. Any failure is an explicit safe fallback."""
    candidates = strategy.get("candidates") if isinstance(strategy, dict) else None
    candidates = candidates if isinstance(candidates, list) else []
    selected = [row for row in candidates[:max(1, min(int(limit), 15))] if isinstance(row, dict) and row.get("Ticker")]
    tickers = [str(row["Ticker"]) for row in selected]
    if not tickers:
        return {"ok": False, "error": "没有可供 AI 复排的候选", "ranking": [], "items": []}

    started = time.perf_counter()
    provider = ""
    model = ""
    try:
        provider = _resolve_provider()
        model = _resolve_model(provider)
        cfg = build_llm_cfg(model, provider)
        generate_cfg = dict(cfg.get("generate_cfg") or {})
        generate_cfg.update({
            "temperature": float(os.environ.get("SCREENING_RERANK_TEMPERATURE", "0.1")),
            "max_retries": 0,
            "max_input_tokens": 9000,
        })
        cfg["generate_cfg"] = generate_cfg
        messages = [
            {"role": "system", "content": "Return only a strict JSON object. You are not an autonomous agent."},
            {"role": "user", "content": _build_prompt(strategy, selected)},
        ]
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screening-rerank")
        future = executor.submit(_call_model, messages, cfg)
        timeout = max(1, int(os.environ.get("SCREENING_RERANK_TIMEOUT_SECONDS", "45")))
        try:
            raw_text = future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(f"AI 复排在 {timeout} 秒后超时") from exc
        executor.shutdown(wait=True)
        payload = _parse_json(raw_text)
        ranking = payload.get("ranked_tickers")
        items = payload.get("items")
        if not isinstance(ranking, list) or [str(value) for value in ranking] != list(dict.fromkeys(str(value) for value in ranking)):
            raise ValueError("AI 返回了重复或无效的 ticker 排名")
        if set(str(value) for value in ranking) != set(tickers) or len(ranking) != len(tickers):
            raise ValueError("AI 复排只能包含原始候选 ticker")
        by_ticker = {str(item.get("ticker")): item for item in items if isinstance(item, dict) and item.get("ticker")} if isinstance(items, list) else {}
        if set(by_ticker) != set(tickers):
            raise ValueError("AI 复排缺少候选理由")
        clean_items = [
            {"ticker": ticker, "reason": _safe_text(by_ticker[ticker].get("reason"), 120), "risk": _safe_text(by_ticker[ticker].get("risk"), 80)}
            for ticker in ranking
        ]
        return {"ok": True, "provider": provider, "model": model, "ranking": [str(ticker) for ticker in ranking], "items": clean_items, "elapsed_seconds": round(time.perf_counter() - started, 2)}
    except Exception as exc:
        return {"ok": False, "provider": provider, "model": model, "error": str(exc), "ranking": [], "items": [], "elapsed_seconds": round(time.perf_counter() - started, 2)}
