# -*- coding: utf-8 -*-
"""AI Gap Analyzer（修改计划第六轮第 18 节）。

第一轮搜索后，由 AI 做一次缺口检查：

输入：
    - 每个 ticker 的 planned_needs（event_need 列表）
    - found_events（第一轮已找到的事件）

输出：
    - additional_search_required: bool
    - missing_needs: 第一轮没找到对应事件的 event_need
    - queries: 补搜 query 列表（最多 6 条）

限制：
    - 最多补搜 1 轮
    - 最多新增 6 条 Query
    - 复用 Portfolio Agent 模型，但独立调用
"""
from __future__ import annotations

import json
import os
from typing import Any

from .config import build_llm_cfg
from .research_plan_schema import (
    ALLOWED_EVENT_NEEDS,
    ALLOWED_LOOKBACK_DAYS,
    is_valid_event_need,
    is_valid_lookback_days,
)


# 补搜预算（修改计划第 18 节）
MAX_GAP_SEARCH_ROUNDS = 1
MAX_GAP_QUERIES = int(os.environ.get("PORTFOLIO_RESEARCH_GAP_MAX_QUERIES", "6"))


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
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) > 2:
            s = "\n".join(lines[1:-1]).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Gap Analyzer 未返回 JSON 对象")
    parsed = json.loads(s[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Gap Analyzer 输出不是对象")
    return parsed


def _llm_configured(provider: str) -> bool:
    provider = (provider or "dashscope").lower()
    if provider == "dashscope":
        return bool(os.environ.get("DASHSCOPE_API_KEY"))
    if provider == "deepseek":
        return bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    if provider == "openai_compatible":
        return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("QWEN_API_KEY"))
    return False


def _build_gap_prompt(
    ticker_gaps: list[dict[str, Any]],
) -> str:
    """构建 Gap Analyzer 的 user prompt。"""
    return (
        "你是投资研究缺口分析器。第一轮搜索已完成，现在检查是否有遗漏的研究需求。\n\n"
        "对每个 ticker：\n"
        "1. 比较 planned_needs 与 found_events，找出 missing_needs（第一轮没有找到对应事件的 event_need）；\n"
        "2. 对每个 missing_need，生成最多 2 条补搜 query（英文或中文，与 ticker 语言一致）；\n"
        "3. 如果所有 planned_needs 都已找到对应事件，则 additional_search_required=false。\n\n"
        "硬性要求：\n"
        "- 最多补搜 1 轮；\n"
        f"- 最多新增 {MAX_GAP_QUERIES} 条 query（跨所有 ticker 合计）；\n"
        "- lookback_days 只能选 7/14/30/45/120/365；\n"
        "- 不生成投资建议；\n"
        "- 返回严格 JSON。\n\n"
        "输出 Schema：\n"
        "{\n"
        "  \"additional_search_required\": true|false,\n"
        "  \"ticker_gaps\": [\n"
        "    {\n"
        "      \"ticker\": \"<TICKER>\",\n"
        "      \"missing_needs\": [\"<event_need>\", ...],\n"
        "      \"queries\": [\n"
        "        {\"query\": \"<query>\", \"lookback_days\": 30, \"language\": \"en|zh-CN\", \"reason_zh\": \"...\"}\n"
        "      ]\n"
        "    }\n"
        "  ],\n"
        "  \"total_new_queries\": <int>\n"
        "}\n\n"
        "允许的 event_need：" + ", ".join(sorted(ALLOWED_EVENT_NEEDS)) + "\n\n"
        "输入数据（每个 ticker 的 planned_needs 与已找到事件）：\n"
        + json.dumps(ticker_gaps, ensure_ascii=False, default=str)
    )


def _compute_ticker_gaps(
    plan: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    first_pass: bool = False,
) -> list[dict[str, Any]]:
    """确定性计算每个 ticker 的 planned_needs 与 found_events 缺口（供 LLM 输入）。

    §16 修复：仅基于 materiality_accepted 且未被拒绝的证据判断"已找到事件"。
    """
    ticker_gaps: list[dict[str, Any]] = []
    for t in plan.get("tickers") or []:
        ticker = str(t.get("ticker") or "").upper()
        planned_needs: list[str] = []
        for q in t.get("research_questions") or []:
            en = str(q.get("event_need") or "")
            if en and en not in planned_needs:
                planned_needs.append(en)
        # 找到该 ticker 的已发现事件（§16：仅 accepted 证据）
        found_events: list[dict[str, Any]] = []
        found_needs: set[str] = set()
        for ev in evidence:
            if str(ev.get("ticker") or "").upper() != ticker:
                continue
            # P0-4: first_pass 模式使用轻量判断；正式模式检查 materiality
            if first_pass:
                en = str(ev.get("event_hint") or ev.get("event_need") or "")
            else:
                if not ev.get("materiality_accepted"):
                    continue
                if ev.get("accept") is False:
                    continue
                en = str(ev.get("event_hint") or ev.get("event_need") or "")
            found_events.append({
                "event_need": en,
                "date": ev.get("published_date") or ev.get("event_date"),
                "title": ev.get("title"),
            })
            if en:
                found_needs.add(en)
        missing = [n for n in planned_needs if n not in found_needs]
        ticker_gaps.append({
            "ticker": ticker,
            "planned_needs": planned_needs,
            "found_needs": sorted(found_needs),
            "missing_needs": missing,
            "found_events": found_events[:5],
        })
    return ticker_gaps


def analyze_research_gap(
    plan: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    model: str,
    provider: str,
    save_path: "os.PathLike[str] | str | None" = None,
) -> dict[str, Any]:
    """第一轮搜索后的缺口分析。

    返回结构：
        {
            "additional_search_required": bool,
            "ticker_gaps": [...],
            "total_new_queries": int,
            "gap_mode": "ai" | "deterministic" | "skipped",
            "errors": [...],
        }
    """
    diagnostics: dict[str, Any] = {
        "additional_search_required": False,
        "ticker_gaps": [],
        "total_new_queries": 0,
        "gap_mode": None,
        "errors": [],
    }

    # P0-4: first_pass=True — raw results 无 materiality 字段，使用轻量匹配
    ticker_gaps = _compute_ticker_gaps(plan, evidence, first_pass=True)
    # P0-4: 记录 Gap 诊断
    diagnostics["planned_needs"] = sum(len(g.get("planned_needs") or []) for g in ticker_gaps)
    diagnostics["first_pass_found_needs"] = sum(len(g.get("found_needs") or []) for g in ticker_gaps)
    diagnostics["missing_needs"] = sum(len(g.get("missing_needs") or []) for g in ticker_gaps)
    diagnostics["ticker_gaps"] = ticker_gaps
    has_missing = any(g.get("missing_needs") for g in ticker_gaps)
    if not has_missing:
        diagnostics["gap_mode"] = "skipped"
        diagnostics["additional_search_required"] = False
        if save_path is not None:
            _save(diagnostics, save_path)
        return diagnostics

    # LLM 不可用 → 用确定性补搜
    if not _llm_configured(provider):
        diagnostics["gap_mode"] = "deterministic"
        queries = _deterministic_gap_queries(ticker_gaps)
        diagnostics["additional_search_required"] = bool(queries)
        diagnostics["total_new_queries"] = len(queries)
        diagnostics["gap_queries"] = queries
        for g in diagnostics["ticker_gaps"]:
            g["queries"] = [q for q in queries if q.get("ticker") == g["ticker"]]
        if save_path is not None:
            _save(diagnostics, save_path)
        return diagnostics

    # 主路径：调用 LLM
    prompt = _build_gap_prompt(ticker_gaps)
    try:
        from qwen_agent.llm import get_chat_model
        llm = get_chat_model(build_llm_cfg(model=model, provider=provider))
        response = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            extra_generate_cfg={"temperature": 0.1},
        )
        parsed = _parse_json(_content(response))
        # 校验 LLM 输出
        validated_queries: list[dict[str, Any]] = []
        total = 0
        for g in parsed.get("ticker_gaps") or []:
            ticker = str(g.get("ticker") or "").upper()
            missing = [str(n) for n in (g.get("missing_needs") or []) if is_valid_event_need(n)]
            queries_raw = g.get("queries") or []
            ticker_queries: list[dict[str, Any]] = []
            for q in queries_raw:
                if total >= MAX_GAP_QUERIES:
                    break
                qtext = str(q.get("query") or "").strip()
                if len(qtext) < 5 or len(qtext) > 240:
                    continue
                lookback = q.get("lookback_days")
                if not is_valid_lookback_days(lookback):
                    lookback = 30
                language = str(q.get("language") or "en").strip()
                # P0-5: 优先使用 LLM 返回的 event_need，缺失时 fallback 到对应的 missing need
                q_event_need = str(q.get("event_need") or "").strip()
                if not is_valid_event_need(q_event_need):
                    # 尝试匹配对应的 missing_need（按 query 索引）
                    idx = min(len(ticker_queries), len(missing) - 1) if missing else -1
                    q_event_need = missing[idx] if idx >= 0 else "general_event"
                ticker_queries.append({
                    "query": qtext,
                    "lookback_days": int(lookback),
                    "language": language,
                    "reason_zh": str(q.get("reason_zh") or ""),
                    "ticker": ticker,
                    "event_need": q_event_need,
                })
                total += 1
            validated_queries.extend(ticker_queries)
            # 更新对应 ticker_gaps 的 queries
            for dg in diagnostics["ticker_gaps"]:
                if dg["ticker"] == ticker:
                    dg["missing_needs"] = missing
                    dg["queries"] = ticker_queries
        diagnostics["additional_search_required"] = bool(validated_queries)
        diagnostics["total_new_queries"] = len(validated_queries)
        diagnostics["gap_mode"] = "ai"
        diagnostics["validated_queries"] = validated_queries
        diagnostics["gap_queries"] = validated_queries
    except Exception as exc:  # noqa: BLE001
        diagnostics["errors"].append(f"gap_analyzer_llm_failed: {type(exc).__name__}: {exc}")
        # 降级到确定性
        diagnostics["gap_mode"] = "deterministic"
        queries = _deterministic_gap_queries(ticker_gaps)
        diagnostics["additional_search_required"] = bool(queries)
        diagnostics["total_new_queries"] = len(queries)
        diagnostics["gap_queries"] = queries
        for g in diagnostics["ticker_gaps"]:
            g["queries"] = [q for q in queries if q.get("ticker") == g["ticker"]]

    if save_path is not None:
        _save(diagnostics, save_path)
    return diagnostics


def _deterministic_gap_queries(ticker_gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确定性补搜：对每个 missing_need 生成 1 条通用 query。"""
    queries: list[dict[str, Any]] = []
    for g in ticker_gaps:
        ticker = g.get("ticker")
        if not ticker:
            continue
        for need in g.get("missing_needs") or []:
            if len(queries) >= MAX_GAP_QUERIES:
                break
            # 简单模板：ticker + event_need keywords + latest
            keywords = need.replace("_", " ")
            queries.append({
                "query": f"{ticker} {keywords} latest 2026",
                "lookback_days": 30,
                "language": "en",
                "reason_zh": f"第一轮未找到 {need} 相关事件，补搜。",
                "ticker": ticker,
                "event_need": need,
            })
    return queries[:MAX_GAP_QUERIES]


def _save(diagnostics: dict[str, Any], path: "os.PathLike[str] | str") -> None:
    try:
        import pathlib
        pathlib.Path(path).write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
