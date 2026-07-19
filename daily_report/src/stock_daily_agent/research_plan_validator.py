# -*- coding: utf-8 -*-
"""Research Plan Validator（修改计划第六轮第 2.2 / 7-9 节；第七轮 P0-6 结构化 + 自动清洗）。

AI Planner 不能无限自由生成搜索计划。本模块对 Planner 输出做严格校验并自动清洗
非致命错误，仅致命错误才返回给上层触发 fallback/Repair/Salvage。

结构化错误格式（计划第 8 节）：
    {"path": "tickers[ORCL].research_questions[1].lookback_days",
     "code": "invalid_lookback_days", "message": "...",
     "received": 60, "allowed": [7,14,30,45,120,365], "fatal": False}

自动清洗的非致命错误（计划 §8.4）：
- lookback_days 不在允许档位 → 映射到最近档位
- query 中 site:domain 前缀 → 移入 preferred_domains
- 重复 query → 去重
- reason_zh 过短 → 补默认
- primary_language 缺失/不一致 → Language Router 补齐
- 单 ticker 问题数超限 → 保留最高 priority
- 单 question query 数超限 → 保留前 N 个
- 总 query 超预算 → 按 priority 修剪
- lane 不合法 → 默认 news
- research_priority 不合法 → 默认 medium
- preferred_domains 非法 hostname → 跳过

致命错误（仅这些触发 fallback，计划 §8.4）：
- plan 不是 dict（无法解析结构）
- 未知 ticker（不在 top_risk_tickers）
- 无任何合法 query
- 校验后无任何合法 ticker
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

# site: 前缀匹配（计划 §8.4）
_SITE_PREFIX_RE = re.compile(r"\bsite:([a-z0-9.-]+(?:\.[a-z]{2,}))\b", re.I)
_SITE_PREFIX_START_RE = re.compile(r"^site:([a-z0-9.-]+(?:\.[a-z]{2,}))\s+", re.I)

# 可用事件类型兜底
_DEFAULT_EVENT_NEED = "news_event"
_DEFAULT_REASON_ZH = "根据持仓风险排序自动分配的研究问题"


# ── 结构化错误 ──────────────────────────────────────────────

def _err(
    path: str,
    code: str,
    message: str,
    *,
    received: Any = None,
    allowed: Any = None,
    fatal: bool = True,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "path": path,
        "code": code,
        "message": message,
    }
    if received is not None:
        d["received"] = received
    if allowed is not None:
        d["allowed"] = allowed
    d["fatal"] = fatal
    return d


# ── 自动清洗辅助 ────────────────────────────────────────────

def _nearest_lookback(value: int) -> int:
    """将非法 lookback 映射到最近允许档位（计划 §8.4）。"""
    allowed = sorted(ALLOWED_LOOKBACK_DAYS)
    return min(allowed, key=lambda a: abs(a - value))


def _extract_site_from_query(query: str) -> tuple[str, list[str]]:
    """从 query 文本中提取 site:domain 前缀，返回 (cleaned_query, [domains])。

    计划 §8.4：site:domain 从 query 移出写入 preferred_domains。
    """
    # 开头的 site:domain → 提取
    m = _SITE_PREFIX_START_RE.match(query)
    if m:
        domain = m.group(1).lower()
        cleaned = query[m.end():].strip()
        return cleaned, [domain]
    # 中间的 site:domain → 提取后重新拼接（移除）
    parts = _SITE_PREFIX_RE.split(query)
    domains: list[str] = []
    cleaned_parts: list[str] = []
    i = 0
    while i < len(parts):
        if i % 2 == 1:
            # 奇数位是匹配的 domain
            domains.append(parts[i].lower())
        else:
            if parts[i].strip():
                cleaned_parts.append(parts[i].strip())
        i += 1
    cleaned = " ".join(cleaned_parts).strip()
    return cleaned, list(set(domains))


def _dedup_queries(queries: list[str]) -> tuple[list[str], int]:
    """去重 query 列表（保留首次出现顺序），返回 (unique, removed_count)。"""
    seen: set[str] = set()
    result: list[str] = []
    removed = 0
    for q in queries:
        q_norm = q.strip().lower()
        if q_norm in seen:
            removed += 1
        else:
            seen.add(q_norm)
            result.append(q.strip())
    return result, removed


def _trim_questions_by_priority(
    questions: list[dict[str, Any]], max_count: int,
) -> tuple[list[dict[str, Any]], int]:
    """按 priority 升序保留前 max_count 个问题（计划 §8.4）。"""
    if len(questions) <= max_count:
        return list(questions), 0
    sorted_qs = sorted(questions, key=lambda q: int(q.get("priority") or 999))
    removed = len(questions) - max_count
    return sorted_qs[:max_count], removed


def _trim_queries_by_order(
    queries: list[str], max_count: int,
) -> tuple[list[str], int]:
    """保留前 max_count 个 query，移除多余（计划 §8.4）。"""
    if len(queries) <= max_count:
        return list(queries), 0
    removed = len(queries) - max_count
    return queries[:max_count], removed


def _is_valid_hostname(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    s = value.strip().lower()
    if not re.match(r"^[a-z0-9.-]+$", s):
        return False
    if "." not in s or s.startswith(".") or s.endswith("."):
        return False
    return True


def _is_valid_query_text(value: Any, *, allow_site_prefix: bool = False) -> tuple[bool, str]:
    if not isinstance(value, str):
        return False, "query 不是字符串"
    s = value.strip()
    if len(s) < 5:
        return False, "query 过短（< 5 字符）"
    if len(s) > 240:
        return False, "query 过长（> 240 字符）"
    # site: 前缀在允许时不阻止（待 _extract_site_from_query 清洗）
    forbidden = tuple(p for p in _QUERY_FORBIDDEN_PREFIXES
                      if not (allow_site_prefix and p == "site:"))
    if any(s.startswith(p) for p in forbidden):
        return False, f"query 不得以 {sorted(forbidden)} 之一开头（请放入 preferred_domains/lane）"
    if re.match(r"^https?://", s, re.I):
        return False, "query 不得是裸 URL"
    return True, ""


# ── 公开类型 ───────────────────────────────────────────────

class ResearchPlanValidationError(RuntimeError):
    """Plan Validator 不可恢复错误（理论上不会抛出，调用方应使用 validate 返回值）。"""


# ── 主入口 ──────────────────────────────────────────────────

def validate_research_plan(
    plan: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    ranking: dict[str, Any] | None = None,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
    top_risk_tickers: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """校验并自动清洗 Planner 输出（第七轮 P0-6 重构）。

    返回 (sanitized_plan, fatal_errors)：
    - fatal_errors 为空 → 校验通过，sanitized_plan 可直接使用；
    - fatal_errors 非空 → 存在致命错误，上层应触发 Repair / Salvage / fallback。

    sanitized["warnings"] 包含非致命问题的自动清洗记录。
    errors 中每项为 dict：{path, code, message, received?, allowed?, fatal?}。
    """
    fatal_errors: list[dict[str, Any]] = []
    warnings: list[str] = []

    if not isinstance(plan, dict):
        return {}, [_err("plan", "not_a_dict", "plan 不是对象", received=type(plan).__name__)]

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
        fatal_errors.append(_err("plan.tickers", "not_array", "plan.tickers 不是数组"))
        raw_tickers = []

    top_risk_set = {str(t).upper() for t in top_risk}
    seen_tickers: set[str] = set()
    total_queries = 0
    sanitized_tickers: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_tickers):
        if not isinstance(raw, dict):
            fatal_errors.append(_err(
                f"plan.tickers[{idx}]", "not_object",
                f"plan.tickers[{idx}] 不是对象",
            ))
            continue

        ticker = str(raw.get("ticker") or "").strip().upper()
        ticker_path = f"plan.tickers[{ticker or idx}]"
        if not ticker:
            fatal_errors.append(_err(
                f"plan.tickers[{idx}].ticker", "empty_ticker",
                "ticker 为空",
            ))
            continue
        if top_risk_set and ticker not in top_risk_set:
            fatal_errors.append(_err(
                f"plan.tickers[{ticker}].ticker", "unknown_ticker",
                f"ticker={ticker} 不在 top_risk_tickers 内；Planner 不得调查未列入风险榜的标的",
                received=ticker, allowed=sorted(top_risk_set),
            ))
            continue
        if ticker in seen_tickers:
            warnings.append(f"重复 ticker={ticker}，跳过")
            continue
        seen_tickers.add(ticker)

        # 语言策略：缺失/不一致 → 自动清洗（计划 §8.4）
        lang_decision = determine_search_language(ticker, instrument_metadata)
        expected_lang = lang_decision["primary_language"]
        declared_lang = str(raw.get("primary_language") or "").strip()
        if declared_lang and declared_lang != expected_lang:
            warnings.append(
                f"{ticker_path}.primary_language={declared_lang} "
                f"与 Language Router 决策 {expected_lang} 不一致（自动修正）"
            )
        primary_language = expected_lang

        # research_priority：非法 → 默认 medium
        raw_priority = str(raw.get("research_priority") or "medium").strip().lower()
        if not is_valid_research_priority(raw_priority):
            warnings.append(
                f"{ticker_path}.research_priority={raw_priority} 不合法，默认 medium"
            )
            priority = "medium"
        else:
            priority = raw_priority

        # research_questions
        raw_questions = raw.get("research_questions") or []
        if not isinstance(raw_questions, list):
            fatal_errors.append(_err(
                f"{ticker_path}.research_questions", "not_array",
                f"{ticker_path}.research_questions 不是数组",
            ))
            continue
        if not raw_questions:
            fatal_errors.append(_err(
                f"{ticker_path}.research_questions", "empty", "research_questions 为空",
            ))
            continue

        # 问题数超限 → 按 priority 修剪（计划 §8.4）
        if len(raw_questions) > PLANNER_MAX_QUESTIONS_PER_TICKER:
            warnings.append(
                f"{ticker_path} 研究问题数 {len(raw_questions)} "
                f"超过上限 {PLANNER_MAX_QUESTIONS_PER_TICKER}，按 priority 修剪"
            )
            # 临时补 priority 用于排序
            for qi, q in enumerate(raw_questions):
                if isinstance(q, dict):
                    q["priority"] = int(q.get("priority") or (qi + 1))
            raw_questions, _ = _trim_questions_by_priority(
                raw_questions, PLANNER_MAX_QUESTIONS_PER_TICKER,
            )

        sanitized_questions: list[dict[str, Any]] = []
        seen_question_ids: set[str] = set()
        for q_idx, q_raw in enumerate(raw_questions):
            if not isinstance(q_raw, dict):
                warnings.append(f"{ticker_path}.research_questions[{q_idx}] 不是对象，跳过")
                continue

            qid = str(q_raw.get("question_id") or f"{ticker}_Q{q_idx + 1}").strip()
            q_path = f"{ticker_path}.research_questions[{q_idx}]"
            if qid in seen_question_ids:
                warnings.append(f"{q_path} 重复 question_id={qid}，分配新 ID")
                qid = f"{ticker}_Q{q_idx + 1}_{len(seen_question_ids)}"
            seen_question_ids.add(qid)

            # event_need：非法 → 跳过该问题
            event_need = str(q_raw.get("event_need") or _DEFAULT_EVENT_NEED).strip()
            if not is_valid_event_need(event_need):
                fatal_errors.append(_err(
                    f"{q_path}.event_need", "invalid_event_need",
                    f"event_need={event_need} 不在 allowlist",
                    received=event_need, allowed=sorted(ALLOWED_EVENT_NEEDS),
                ))
                continue

            # lane：非法 → 默认 news（计划 §8.4）
            lane = str(q_raw.get("lane") or "news").strip()
            if not is_valid_lane(lane):
                warnings.append(
                    f"{q_path}.lane={lane} 不合法，默认 news"
                )
                lane = "news"

            # lookback_days：非法 → 最近档位（计划 §8.4）
            lookback_raw = q_raw.get("lookback_days")
            if not is_valid_lookback_days(lookback_raw):
                new_lb = _nearest_lookback(int(lookback_raw) if lookback_raw is not None else 45)
                warnings.append(
                    f"{q_path}.lookback_days={lookback_raw} 不在允许档位，"
                    f"自动映射 {lookback_raw}→{new_lb}"
                )
                lookback_int = new_lb
            else:
                lookback_int = int(lookback_raw)

            # reason_zh：过短 → 补默认（计划 §8.4）
            reason_zh = str(q_raw.get("reason_zh") or "").strip()
            if len(reason_zh) < 8:
                warnings.append(
                    f"{q_path}.reason_zh 过短（{len(reason_zh)} 字符），补默认理由"
                )
                reason_zh = _DEFAULT_REASON_ZH

            # queries
            queries_raw = q_raw.get("queries") or []
            if not isinstance(queries_raw, list) or not queries_raw:
                fatal_errors.append(_err(
                    f"{q_path}.queries", "empty_queries",
                    f"{q_path}.queries 为空",
                ))
                continue

            # query 数超限 → 保留前 N 个（计划 §8.4）
            if len(queries_raw) > PLANNER_MAX_QUERIES_PER_QUESTION:
                warnings.append(
                    f"{q_path} query 数 {len(queries_raw)} "
                    f"超过上限 {PLANNER_MAX_QUERIES_PER_QUESTION}，保留前 {PLANNER_MAX_QUERIES_PER_QUESTION} 个"
                )
                queries_raw, _ = _trim_queries_by_order(queries_raw, PLANNER_MAX_QUERIES_PER_QUESTION)

            # 逐 query 校验 + site: 清洗（计划 §8.4）
            cleaned_queries: list[str] = []
            site_domains: list[str] = []
            for q_text in queries_raw:
                # 先提取 site:domain 前缀
                q_no_site, extracted_domains = _extract_site_from_query(str(q_text or ""))
                if extracted_domains:
                    site_domains.extend(extracted_domains)
                    warnings.append(
                        f"{q_path} query 含 site: 前缀，已移入 preferred_domains: {extracted_domains}"
                    )
                ok, msg = _is_valid_query_text(q_no_site, allow_site_prefix=True)
                if not ok:
                    warnings.append(
                        f"{q_path} query 校验失败：{msg}；query={q_no_site!r}"
                    )
                    continue
                cleaned_queries.append(q_no_site.strip())

            # 去重（计划 §8.4）
            if len(cleaned_queries) > 1:
                cleaned_queries, removed = _dedup_queries(cleaned_queries)
                if removed > 0:
                    warnings.append(f"{q_path} query 去重：移除 {removed} 个重复项")

            if not cleaned_queries:
                fatal_errors.append(_err(
                    f"{q_path}.queries", "no_valid_queries",
                    f"{q_path} 清洗后无合法 query",
                ))
                continue
            total_queries += len(cleaned_queries)

            # preferred_domains（可空；非法 hostname → 跳过）
            preferred_domains_raw = q_raw.get("preferred_domains") or []
            if not isinstance(preferred_domains_raw, list):
                warnings.append(f"{q_path}.preferred_domains 不是数组，清空")
                preferred_domains_raw = []
            preferred_domains: list[str] = []
            for d in list(preferred_domains_raw) + site_domains:
                if not _is_valid_hostname(d):
                    warnings.append(f"{q_path}.preferred_domains={d!r} 不是合法 hostname，跳过")
                    continue
                d_lower = str(d).strip().lower()
                if d_lower not in preferred_domains:
                    preferred_domains.append(d_lower)

            # required_entities：Equity 至少 1 条（可空时只 warning，不死磕）
            required_entities_raw = q_raw.get("required_entities") or []
            if not isinstance(required_entities_raw, list):
                warnings.append(f"{q_path}.required_entities 不是数组，清空")
                required_entities_raw = []
            required_entities = [str(e).strip() for e in required_entities_raw if str(e).strip()]
            meta = instrument_metadata.get(ticker, {}) or {}
            itype = str(meta.get("instrument_type") or "UNKNOWN").upper()
            if itype == "EQUITY" and not required_entities:
                warnings.append(
                    f"{q_path} Equity 标的 required_entities 为空，"
                    f"补 ticker {ticker} 作为最低实体"
                )
                required_entities.append(ticker)

            # exclude_terms（可空）
            exclude_terms_raw = q_raw.get("exclude_terms") or []
            if not isinstance(exclude_terms_raw, list):
                warnings.append(f"{q_path}.exclude_terms 不是数组，清空")
                exclude_terms_raw = []
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
            fatal_errors.append(_err(
                ticker_path, "no_valid_questions",
                f"{ticker_path} 校验后无合法研究问题",
            ))
            continue

        sanitized_tickers.append({
            "ticker": ticker,
            "research_priority": priority,
            "primary_language": primary_language,
            "research_questions": sanitized_questions,
        })

    # 总 query 超预算 → 按 priority 修剪（计划 §8.4）
    if total_queries > PLANNER_MAX_TOTAL_QUERIES:
        warnings.append(
            f"总 query 数 {total_queries} 超过预算 {PLANNER_MAX_TOTAL_QUERIES}，"
            f"按 ticker priority 修剪低优先级问题"
        )
        # 收集所有 ticker 的所有 question（扁平化）
        all_qs: list[tuple[int, int, int, str]] = []  # (ticker_priority_rank, q_priority, q_idx, ticker)
        for ti, st in enumerate(sanitized_tickers):
            for qi, sq in enumerate(st["research_questions"]):
                all_qs.append((ti, int(sq.get("priority") or qi + 1), qi, st["ticker"]))
        all_qs.sort(key=lambda x: (x[0], x[1]))
        # 从低优先级开始移除，直到总 query 数 ≤ 预算
        removed_tqs: list[tuple[str, str]] = []
        while total_queries > PLANNER_MAX_TOTAL_QUERIES and all_qs:
            ti, _, qi, ticker = all_qs.pop()
            st = sanitized_tickers[ti]
            if qi < len(st["research_questions"]):
                q_obj = st["research_questions"][qi]
                qc = len(q_obj["queries"])
                total_queries -= qc
                removed_tqs.append((ticker, q_obj.get("question_id", "")))
                st["research_questions"].pop(qi)
        if removed_tqs:
            warnings.append(
                f"已移除 {len(removed_tqs)} 个低优先级问题以达到 query 预算"
            )
            # 清理空 ticker
            for st in list(sanitized_tickers):
                if not st["research_questions"]:
                    sanitized_tickers.remove(st)

    # macro_questions（可空）
    macro_raw = plan.get("macro_questions") or []
    sanitized_macro: list[dict[str, Any]] = []
    if isinstance(macro_raw, list):
        for m_idx, m in enumerate(macro_raw):
            if not isinstance(m, dict):
                warnings.append(f"plan.macro_questions[{m_idx}] 不是对象")
                continue
            m_path = f"plan.macro_questions[{m_idx}]"

            event_need = str(m.get("event_need") or "macro_driver").strip()
            if not is_valid_event_need(event_need):
                warnings.append(f"{m_path}.event_need={event_need} 不在 allowlist，默认 macro_driver")
                event_need = "macro_driver"

            lane = str(m.get("lane") or "macro").strip()
            if not is_valid_lane(lane):
                warnings.append(f"{m_path}.lane={lane} 不合法，默认 macro")
                lane = "macro"

            lookback_raw = m.get("lookback_days")
            if not is_valid_lookback_days(lookback_raw):
                new_lb = _nearest_lookback(int(lookback_raw) if lookback_raw is not None else 45)
                warnings.append(f"{m_path}.lookback_days={lookback_raw} 不在允许档位，自动映射 {lookback_raw}→{new_lb}")
                lookback_int = new_lb
            else:
                lookback_int = int(lookback_raw)

            queries_raw = m.get("queries") or []
            cleaned: list[str] = []
            site_domains_m: list[str] = []
            for q_text in queries_raw:
                q_no_site, d = _extract_site_from_query(str(q_text or ""))
                site_domains_m.extend(d)
                ok, msg = _is_valid_query_text(q_no_site, allow_site_prefix=True)
                if not ok:
                    warnings.append(f"{m_path} query 校验失败：{msg}")
                    continue
                cleaned.append(q_no_site.strip())
            if not cleaned:
                continue
            total_queries += len(cleaned)

            sanitized_macro.append({
                "question_id": str(m.get("question_id") or f"MACRO_Q{m_idx + 1}"),
                "event_need": event_need,
                "reason_zh": str(m.get("reason_zh") or "宏观层面因素影响全部持仓的风险偏好"),
                "lane": lane,
                "lookback_days": lookback_int,
                "queries": cleaned,
                "preferred_domains": [
                    str(d).strip().lower() for d in (m.get("preferred_domains") or []) + site_domains_m
                    if _is_valid_hostname(d)
                ],
                "required_entities": [],
                "exclude_terms": [],
                "priority": int(m.get("priority") or (m_idx + 1)),
            })
        sanitized["macro_questions"] = sanitized_macro

    sanitized["tickers"] = sanitized_tickers
    sanitized["total_queries"] = total_queries
    sanitized["warnings"] = warnings

    # 致命：校验后无任何合法 ticker（计划 §8.4）
    if not sanitized_tickers:
        fatal_errors.append(_err(
            "plan.tickers", "no_valid_tickers",
            "校验后没有任何合法 ticker；Plan 不可用",
        ))

    return sanitized, fatal_errors
