# -*- coding: utf-8 -*-
"""Research Plan Validator（修改计划第六轮第 2.2 / 7-9 节）。

AI Planner 不能无限自由生成搜索计划。本模块对 Planner 输出做严格校验：

- Event Need 类型必须在 allowlist；
- 时间窗口必须在允许档位（7/14/30/45/120/365）；
- Query 数量不超过每个 ticker / 每个问题 / 总预算；
- lane 必须合法；
- primary_language 必须与 Language Router 决策一致（A 股 = zh-CN，非 A 股 = en）；
- preferred_domains 必须是合法 hostname；
- required_entities 至少 1 条；
- queries 不得为空字符串、不得包含明显非法操作符；
- 总 tickers 数不超过 ranking 提供的 top_risk_tickers。

校验失败时返回 errors 列表；上层据此降级到 fallback planner。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .research_plan_schema import (
    ALLOWED_EVENT_NEEDS,
    ALLOWED_LANES,
    ALLOWED_LOOKBACK_DAYS,
    ALLOWED_RESEARCH_PRIORITIES,
    ALLOWED_URL_SCHEMES,
    PLANNER_MAX_QUERIES_PER_QUESTION,
    PLANNER_MAX_QUESTIONS_PER_TICKER,
    PLANNER_MAX_TOTAL_QUERIES,
    is_valid_event_need,
    is_valid_lane,
    is_valid_lookback_days,
    is_valid_research_priority,
)
from .research_core.language_router import determine_search_language, is_a_share


# queries 中禁止出现的 Serper/Google 高风险操作符前缀
_QUERY_FORBIDDEN_PREFIXES = ("site:", "inurl:", "intitle:", "filetype:", "OR ")


def _is_valid_hostname(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    s = value.strip().lower()
    # 简单 hostname 校验：仅允许字母数字 . -，至少一个点
    if not re.match(r"^[a-z0-9.-]+$", s):
        return False
    if "." not in s or s.startswith(".") or s.endswith("."):
        return False
    return True


def _is_valid_query_text(value: Any) -> tuple[bool, str]:
    if not isinstance(value, str):
        return False, "query 不是字符串"
    s = value.strip()
    if len(s) < 5:
        return False, "query 过短（< 5 字符）"
    if len(s) > 240:
        return False, "query 过长（> 240 字符）"
    if any(s.startswith(p) for p in _QUERY_FORBIDDEN_PREFIXES):
        return False, f"query 不得以 {sorted(_QUERY_FORBIDDEN_PREFIXES)} 之一开头（请放入 preferred_domains/lane）"
    # 不允许裸 URL 作为 query
    if re.match(r"^https?://", s, re.I):
        return False, "query 不得是裸 URL"
    return True, ""


class ResearchPlanValidationError(RuntimeError):
    """Plan Validator 不可恢复错误（理论上不会抛出，调用方应使用 validate 返回值）。"""


def validate_research_plan(
    plan: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    ranking: dict[str, Any] | None = None,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
    top_risk_tickers: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """校验并清洗 Planner 输出。

    返回 (sanitized_plan, errors)：
    - errors 为空表示通过；上层可直接使用 sanitized_plan。
    - errors 非空表示至少有一项硬伤；上层应降级到 fallback planner，
      但 sanitized_plan 仍尽量保留合法部分（best-effort），便于诊断。
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(plan, dict):
        return {}, ["plan 不是对象"]

    instrument_metadata = instrument_metadata or {}
    ranking = ranking or {}
    top_risk = list(top_risk_tickers or ranking.get("top_risk_tickers") or [])

    # 顶层字段
    plan_version = str(plan.get("plan_version") or "1.0")
    if plan_version != "1.0":
        warnings.append(f"plan_version={plan_version} 不是 1.0，按兼容模式处理")
    sanitized: dict[str, Any] = {
        "plan_version": "1.0",
        "planner_model": plan.get("planner_model") or "same_as_portfolio_agent",
        "tickers": [],
        "macro_questions": [],
        "warnings": warnings,
    }

    raw_tickers = plan.get("tickers") or []
    if not isinstance(raw_tickers, list):
        errors.append("plan.tickers 不是数组")
        raw_tickers = []

    # ticker 白名单：必须是 top_risk 之一（避免 Planner 凭空发明）
    top_risk_set = {str(t).upper() for t in top_risk}
    seen_tickers: set[str] = set()
    total_queries = 0
    sanitized_tickers: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_tickers):
        if not isinstance(raw, dict):
            errors.append(f"plan.tickers[{idx}] 不是对象")
            continue
        ticker = str(raw.get("ticker") or "").strip().upper()
        if not ticker:
            errors.append(f"plan.tickers[{idx}].ticker 为空")
            continue
        if top_risk_set and ticker not in top_risk_set:
            errors.append(
                f"plan.tickers[{idx}].ticker={ticker} 不在 top_risk_tickers 内；"
                f"Planner 不得调查未列入风险榜的标的"
            )
            continue
        if ticker in seen_tickers:
            errors.append(f"plan.tickers 重复 ticker={ticker}")
            continue
        seen_tickers.add(ticker)

        # 语言策略一致性校验（修改计划 2.4）
        lang_decision = determine_search_language(ticker, instrument_metadata)
        expected_lang = lang_decision["primary_language"]
        declared_lang = str(raw.get("primary_language") or "").strip()
        if declared_lang and declared_lang != expected_lang:
            errors.append(
                f"plan.tickers[{ticker}].primary_language={declared_lang} "
                f"与 Language Router 决策 {expected_lang} 不一致 "
                f"(reason={lang_decision['reason']}, market={lang_decision['market']})"
            )
            continue
        # 补齐 primary_language（Planner 可能省略）
        primary_language = declared_lang or expected_lang

        # research_priority
        priority = str(raw.get("research_priority") or "medium").strip().lower()
        if not is_valid_research_priority(priority):
            errors.append(
                f"plan.tickers[{ticker}].research_priority={priority} 不合法；"
                f"允许值：{sorted(ALLOWED_RESEARCH_PRIORITIES)}"
            )
            continue

        # research_questions
        raw_questions = raw.get("research_questions") or []
        if not isinstance(raw_questions, list):
            errors.append(f"plan.tickers[{ticker}].research_questions 不是数组")
            continue
        if len(raw_questions) > PLANNER_MAX_QUESTIONS_PER_TICKER:
            errors.append(
                f"plan.tickers[{ticker}] 研究问题数 {len(raw_questions)} "
                f"超过上限 {PLANNER_MAX_QUESTIONS_PER_TICKER}"
            )
            continue
        if not raw_questions:
            errors.append(f"plan.tickers[{ticker}].research_questions 为空")
            continue

        sanitized_questions: list[dict[str, Any]] = []
        seen_question_ids: set[str] = set()
        for q_idx, q_raw in enumerate(raw_questions):
            if not isinstance(q_raw, dict):
                errors.append(f"plan.tickers[{ticker}].research_questions[{q_idx}] 不是对象")
                continue
            qid = str(q_raw.get("question_id") or f"{ticker}_Q{q_idx + 1}").strip()
            if qid in seen_question_ids:
                errors.append(f"plan.tickers[{ticker}] 重复 question_id={qid}")
                continue
            seen_question_ids.add(qid)

            event_need = str(q_raw.get("event_need") or "").strip()
            if not is_valid_event_need(event_need):
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}].event_need="
                    f"{event_need} 不在 allowlist；"
                    f"Planner 不得发明新 event type"
                )
                continue

            lane = str(q_raw.get("lane") or "news").strip()
            if not is_valid_lane(lane):
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}].lane={lane} 不合法；"
                    f"允许值：{sorted(ALLOWED_LANES)}"
                )
                continue

            lookback = q_raw.get("lookback_days")
            if not is_valid_lookback_days(lookback):
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}].lookback_days="
                    f"{lookback} 不在允许档位 {sorted(ALLOWED_LOOKBACK_DAYS)}"
                )
                continue
            lookback_int = int(lookback)

            reason_zh = str(q_raw.get("reason_zh") or "").strip()
            if len(reason_zh) < 8:
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}].reason_zh "
                    f"过短（< 8 字符），必须说明为什么与当前持仓风险相关"
                )
                continue

            queries_raw = q_raw.get("queries") or []
            if not isinstance(queries_raw, list) or not queries_raw:
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}].queries 为空"
                )
                continue
            if len(queries_raw) > PLANNER_MAX_QUERIES_PER_QUESTION:
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}] query 数 "
                    f"{len(queries_raw)} 超过上限 {PLANNER_MAX_QUERIES_PER_QUESTION}"
                )
                continue

            cleaned_queries: list[str] = []
            for q_text in queries_raw:
                ok, msg = _is_valid_query_text(q_text)
                if not ok:
                    errors.append(
                        f"plan.tickers[{ticker}].research_questions[{q_idx}] "
                        f"query 校验失败：{msg}；query={q_text!r}"
                    )
                    continue
                cleaned_queries.append(str(q_text).strip())
            if not cleaned_queries:
                # 该 question 全部 query 非法，跳过该 question
                continue
            total_queries += len(cleaned_queries)

            # preferred_domains（可空，但若提供必须合法）
            preferred_domains_raw = q_raw.get("preferred_domains") or []
            if not isinstance(preferred_domains_raw, list):
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}].preferred_domains 不是数组"
                )
                continue
            preferred_domains: list[str] = []
            for d in preferred_domains_raw:
                if not _is_valid_hostname(d):
                    errors.append(
                        f"plan.tickers[{ticker}].research_questions[{q_idx}] "
                        f"preferred_domains={d!r} 不是合法 hostname"
                    )
                    continue
                preferred_domains.append(str(d).strip().lower())

            # required_entities：Equity 至少 1 条
            required_entities = q_raw.get("required_entities") or []
            if not isinstance(required_entities, list):
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}].required_entities 不是数组"
                )
                continue
            required_entities = [str(e).strip() for e in required_entities if str(e).strip()]
            meta = instrument_metadata.get(ticker, {}) or {}
            itype = str(meta.get("instrument_type") or "UNKNOWN").upper()
            if itype == "EQUITY" and not required_entities:
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}] "
                    f"Equity 标的 required_entities 不能为空"
                )
                continue

            # exclude_terms（可空）
            exclude_terms_raw = q_raw.get("exclude_terms") or []
            if not isinstance(exclude_terms_raw, list):
                errors.append(
                    f"plan.tickers[{ticker}].research_questions[{q_idx}].exclude_terms 不是数组"
                )
                continue
            exclude_terms = [str(e).strip() for e in exclude_terms_raw if str(e).strip()]

            try:
                priority_int = int(q_raw.get("priority") or (q_idx + 1))
            except (TypeError, ValueError):
                priority_int = q_idx + 1

            sanitized_questions.append({
                "question_id": qid,
                "event_need": event_need,
                "reason_zh": reason_zh,
                "lane": lane,
                "lookback_days": lookback_int,
                "queries": cleaned_queries,
                "preferred_domains": preferred_domains,
                "required_entities": required_entities,
                "exclude_terms": exclude_terms,
                "priority": priority_int,
            })

        if not sanitized_questions:
            # 该 ticker 没有合法的研究问题
            continue

        sanitized_tickers.append({
            "ticker": ticker,
            "research_priority": priority,
            "primary_language": primary_language,
            "research_questions": sanitized_questions,
        })

    # 总 query 预算
    if total_queries > PLANNER_MAX_TOTAL_QUERIES:
        errors.append(
            f"总 query 数 {total_queries} 超过预算 {PLANNER_MAX_TOTAL_QUERIES}"
        )

    # macro_questions（可空，但若提供必须合法）
    macro_raw = plan.get("macro_questions") or []
    if isinstance(macro_raw, list):
        sanitized_macro: list[dict[str, Any]] = []
        for m_idx, m in enumerate(macro_raw):
            if not isinstance(m, dict):
                errors.append(f"plan.macro_questions[{m_idx}] 不是对象")
                continue
            event_need = str(m.get("event_need") or "macro_driver").strip()
            if not is_valid_event_need(event_need):
                errors.append(
                    f"plan.macro_questions[{m_idx}].event_need={event_need} 不在 allowlist"
                )
                continue
            lane = str(m.get("lane") or "macro").strip()
            if not is_valid_lane(lane):
                errors.append(f"plan.macro_questions[{m_idx}].lane={lane} 不合法")
                continue
            lookback = m.get("lookback_days")
            if not is_valid_lookback_days(lookback):
                errors.append(
                    f"plan.macro_questions[{m_idx}].lookback_days={lookback} 不合法"
                )
                continue
            queries_raw = m.get("queries") or []
            cleaned = []
            for q_text in queries_raw:
                ok, msg = _is_valid_query_text(q_text)
                if not ok:
                    errors.append(f"plan.macro_questions[{m_idx}] query 校验失败：{msg}")
                    continue
                cleaned.append(str(q_text).strip())
            if not cleaned:
                continue
            total_queries += len(cleaned)
            sanitized_macro.append({
                "question_id": str(m.get("question_id") or f"MACRO_Q{m_idx + 1}"),
                "event_need": event_need,
                "reason_zh": str(m.get("reason_zh") or "宏观层面因素影响全部持仓的风险偏好"),
                "lane": lane,
                "lookback_days": int(lookback),
                "queries": cleaned,
                "preferred_domains": [
                    str(d).strip().lower() for d in (m.get("preferred_domains") or []) if _is_valid_hostname(d)
                ],
                "required_entities": [],
                "exclude_terms": [],
                "priority": int(m.get("priority") or (m_idx + 1)),
            })
        sanitized["macro_questions"] = sanitized_macro

    if total_queries > PLANNER_MAX_TOTAL_QUERIES and not any("总 query 数" in e for e in errors):
        errors.append(
            f"总 query 数（含 macro）{total_queries} 超过预算 {PLANNER_MAX_TOTAL_QUERIES}"
        )

    sanitized["tickers"] = sanitized_tickers
    sanitized["total_queries"] = total_queries
    sanitized["warnings"] = warnings

    # 至少要有一个 ticker 被合法化
    if not sanitized_tickers:
        errors.append("校验后没有任何合法 ticker；Plan 不可用")

    return sanitized, errors
