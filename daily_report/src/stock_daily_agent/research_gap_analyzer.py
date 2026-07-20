# -*- coding: utf-8 -*-
"""Portfolio research gap analysis.

Raw search results do not automatically satisfy a planned research need.  First-pass
coverage requires a lightweight entity/page/event qualification; final-pass coverage
requires materiality acceptance.  This prevents quote/reference pages from suppressing
precision gap searches.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

from .config import build_llm_cfg
from .research_plan_schema import ALLOWED_EVENT_NEEDS, is_valid_event_need, is_valid_lookback_days
from .research_core.entity_resolution import resolve_primary_entity

MAX_GAP_SEARCH_ROUNDS = 1
MAX_GAP_QUERIES = int(os.environ.get("PORTFOLIO_RESEARCH_GAP_MAX_QUERIES", "6"))

_OFFICIAL_FIRST_NEEDS = {
    "earnings_date", "earnings_results", "guidance", "latest_official_filing",
    "management_change", "capital_raise", "major_contract", "governance",
    "product_event", "merger_acquisition",
}

_EVENT_NEED_KEYWORDS: dict[str, tuple[str, ...]] = {
    "earnings_date": ("earnings date", "report date", "conference call", "financial results", "财报", "业绩发布"),
    "earnings_results": ("earnings", "results", "revenue", "eps", "profit", "loss", "财报", "营收", "利润"),
    "guidance": ("guidance", "outlook", "forecast", "expects", "指引", "展望", "预期"),
    "credit_and_financing": ("credit", "rating", "debt", "bond", "financing", "offering", "融资", "债券", "评级"),
    "merger_acquisition": ("merger", "acquisition", "acquire", "deal", "takeover", "收购", "合并"),
    "management_change": ("ceo", "cfo", "chair", "appoint", "resign", "step down", "管理层", "任命", "辞任"),
    "theme_supply": ("supply", "production", "capacity", "outage", "shortage", "inventory", "供应", "产量", "库存"),
    "theme_policy": ("policy", "regulation", "bill", "tariff", "sanction", "subsidy", "政策", "监管", "法案"),
    "regulatory": ("regulator", "sec", "doj", "ftc", "probe", "investigation", "approval", "监管", "调查", "批准"),
    "crypto_regulation": ("crypto", "bitcoin", "sec", "cftc", "mica", "regulation", "加密", "监管"),
    "product_event": ("launch", "closure", "liquidation", "fee change", "distribution", "推出", "关闭", "费率"),
    "constituent_event": ("rebalance", "reconstitution", "constituent", "addition", "removal", "调仓", "成分股"),
    "analyst_revision": ("analyst", "rating", "price target", "upgrade", "downgrade", "分析师", "目标价", "评级"),
    "aum_change": ("assets under management", "aum", "fund size", "资产管理规模", "基金规模"),
    "capital_raise": ("capital raise", "offering", "share sale", "placement", "增发", "配股", "融资"),
    "commodity_driver": ("commodity", "uranium", "gold", "oil", "copper", "price", "商品", "铀", "黄金", "原油", "铜"),
    "fund_flow": ("fund flow", "inflow", "outflow", "redemption", "申购", "赎回", "资金流"),
    "governance": ("governance", "board", "shareholder", "proxy", "治理", "董事会", "股东"),
    "index_rebalance": ("rebalance", "reconstitution", "index addition", "index removal", "指数调整", "调仓"),
    "latest_official_filing": ("filing", "10-q", "10-k", "8-k", "sec", "annual report", "quarterly report", "公告", "申报"),
    "litigation": ("lawsuit", "litigation", "court", "settlement", "诉讼", "和解"),
    "macro_driver": ("inflation", "interest rate", "yield", "fed", "ecb", "gdp", "宏观", "利率", "通胀"),
    "major_contract": ("contract", "agreement", "order", "award", "partnership", "合同", "订单", "协议"),
    "premium_discount": ("premium", "discount", "nav", "溢价", "折价", "净值"),
    "security_incident": ("cyber", "breach", "hack", "security incident", "网络安全", "数据泄露", "攻击"),
    "trading_volume": ("trading volume", "turnover", "volume", "成交量", "成交额", "换手"),
    "general_event": (),
}


def _content(response: Any) -> str:
    if isinstance(response, list):
        return _content(response[-1]) if response else ""
    value = response.get("content", "") if isinstance(response, dict) else getattr(response, "content", "")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(x.get("text") or x.get("content") or "") for x in value if isinstance(x, dict))
    return str(value or "")


def _parse_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) > 2:
            raw = "\n".join(lines[1:-1]).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Gap Analyzer 未返回 JSON 对象")
    parsed = json.loads(raw[start:end + 1])
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


def _need_matches_text(event_need: str, result: dict[str, Any]) -> bool:
    keywords = _EVENT_NEED_KEYWORDS.get(event_need, ())
    if not keywords:
        return True
    text = " ".join(
        str(result.get(key) or "")
        for key in ("title", "summary", "snippet", "url")
    ).lower()
    return any(keyword in text for keyword in keywords)


def _is_qualified_first_pass_candidate(
    result: dict[str, Any],
    ticker: str,
    event_need: str,
    instrument_metadata: dict[str, dict[str, Any]],
) -> tuple[bool, str]:
    """Cheap qualification used before article fetching/materiality."""
    if not result.get("url") or not result.get("title"):
        return False, "missing_url_or_title"
    if not is_valid_event_need(event_need):
        return False, "invalid_event_need"
    meta = instrument_metadata.get(ticker, {})
    entity = resolve_primary_entity(
        result,
        ticker,
        meta,
        body=str(result.get("summary") or result.get("snippet") or ""),
    )
    if entity.get("is_quote_page"):
        return False, "quote_page"
    if entity.get("is_reference_page"):
        return False, "reference_page"
    if entity.get("entity_role") == "incidental":
        return False, "incidental_entity"
    if float(entity.get("primary_entity_score") or 0.0) < 0.50:
        return False, "weak_entity_match"
    if not _need_matches_text(event_need, result):
        return False, "event_need_text_mismatch"
    return True, "qualified"


def _build_gap_prompt(ticker_gaps: list[dict[str, Any]]) -> str:
    return (
        "你是投资研究缺口分析器。只能为输入 missing_needs 生成补搜 Query。\n\n"
        "每条 Query 必须显式返回 event_need，且 event_need 必须属于该 ticker 的 missing_needs。\n"
        f"跨所有 ticker 最多 {MAX_GAP_QUERIES} 条 Query；lookback_days 只能为 7/14/30/45/120/365。\n"
        "不要生成投资建议，返回严格 JSON。\n\n"
        "输出 Schema：\n"
        "{\"ticker_gaps\":[{\"ticker\":\"AAPL\",\"missing_needs\":[\"earnings_results\"],"
        "\"queries\":[{\"query\":\"...\",\"event_need\":\"earnings_results\","
        "\"lookback_days\":30,\"language\":\"en\",\"reason_zh\":\"...\"}]}]}\n\n"
        "允许的 event_need：" + ", ".join(sorted(ALLOWED_EVENT_NEEDS)) + "\n\n"
        + json.dumps(ticker_gaps, ensure_ascii=False, default=str)
    )


def _compute_ticker_gaps(
    plan: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    first_pass: bool = False,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compute planned/found/missing needs using qualified candidates only."""
    instrument_metadata = instrument_metadata or {}
    ticker_gaps: list[dict[str, Any]] = []
    for ticker_plan in plan.get("tickers") or []:
        ticker = str(ticker_plan.get("ticker") or "").upper()
        planned_needs: list[str] = []
        for question in ticker_plan.get("research_questions") or []:
            need = str(question.get("event_need") or "")
            if need and need not in planned_needs:
                planned_needs.append(need)

        found_events: list[dict[str, Any]] = []
        found_needs: set[str] = set()
        rejected_first_pass: dict[str, int] = {}
        for item in evidence:
            if str(item.get("ticker") or "").upper() != ticker:
                continue
            need = str(item.get("event_hint") or item.get("event_need") or item.get("event_type") or "")
            if need not in planned_needs:
                continue
            if first_pass:
                qualified, reason = _is_qualified_first_pass_candidate(
                    item, ticker, need, instrument_metadata
                )
                if not qualified:
                    rejected_first_pass[reason] = rejected_first_pass.get(reason, 0) + 1
                    continue
            else:
                if not item.get("materiality_accepted"):
                    continue
                if item.get("accept") is False:
                    continue
                if item.get("chronology_conflict"):
                    continue
                if item.get("is_quote_page") or item.get("is_reference_page"):
                    continue
            found_needs.add(need)
            found_events.append({
                "event_need": need,
                "date": item.get("published_date") or item.get("event_date"),
                "title": item.get("title"),
                "evidence_uid": item.get("evidence_uid"),
            })

        missing = [need for need in planned_needs if need not in found_needs]
        meta = instrument_metadata.get(ticker, {}) or {}
        ticker_gaps.append({
            "ticker": ticker,
            "name": str(meta.get("name") or ticker),
            "instrument_type": str(meta.get("instrument_type") or "UNKNOWN").upper(),
            "official_domains": list(meta.get("official_domains") or []),
            "theme": meta.get("theme"),
            "underlying_index": meta.get("underlying_index"),
            "key_drivers": list(meta.get("key_drivers") or []),
            "planned_needs": planned_needs,
            "found_needs": sorted(found_needs),
            "missing_needs": missing,
            "found_events": found_events[:8],
            "first_pass_rejected": rejected_first_pass,
        })
    return ticker_gaps


def _validate_llm_queries(
    parsed: dict[str, Any],
    diagnostics: dict[str, Any],
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    allowed_by_ticker = {
        str(g.get("ticker") or "").upper(): set(g.get("missing_needs") or [])
        for g in diagnostics.get("ticker_gaps") or []
    }
    context_by_ticker = {
        str(g.get("ticker") or "").upper(): g
        for g in diagnostics.get("ticker_gaps") or []
    }
    for group in parsed.get("ticker_gaps") or []:
        ticker = str(group.get("ticker") or "").upper()
        allowed_needs = allowed_by_ticker.get(ticker, set())
        for query in group.get("queries") or []:
            if len(validated) >= MAX_GAP_QUERIES or not isinstance(query, dict):
                break
            text = str(query.get("query") or "").strip()
            need = str(query.get("event_need") or "").strip()
            if len(text) < 5 or len(text) > 240:
                continue
            if not is_valid_event_need(need) or need not in allowed_needs:
                diagnostics["errors"].append(f"gap_query_invalid_event_need:{ticker}:{need or 'missing'}")
                continue
            lookback = query.get("lookback_days")
            if not is_valid_lookback_days(lookback):
                lookback = 30
            context = context_by_ticker.get(ticker, {})
            domains = list(context.get("official_domains") or [])
            if need in _OFFICIAL_FIRST_NEEDS and domains and "site:" not in text.lower():
                domain = str(domains[0]).replace("https://", "").replace("http://", "").split("/", 1)[0]
                text = f"site:{domain} {text}"
            validated.append({
                "query": text,
                "lookback_days": int(lookback),
                "language": str(query.get("language") or "en"),
                "reason_zh": str(query.get("reason_zh") or ""),
                "ticker": ticker,
                "event_need": need,
                "scope": "ticker",
                "lane": "official_and_news",
            })
    return validated


def analyze_research_gap(
    plan: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    model: str,
    provider: str,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
    first_pass: bool = True,
    save_path: "os.PathLike[str] | str | None" = None,
) -> dict[str, Any]:
    """Analyze first-pass or post-materiality research gaps."""
    ticker_gaps = _compute_ticker_gaps(
        plan,
        evidence,
        first_pass=first_pass,
        instrument_metadata=instrument_metadata,
    )
    prefix = "first_pass" if first_pass else "post_materiality"
    diagnostics: dict[str, Any] = {
        "additional_search_required": False,
        "ticker_gaps": ticker_gaps,
        "total_new_queries": 0,
        "gap_mode": None,
        "gap_stage": prefix,
        "errors": [],
        "planned_needs": sum(len(g.get("planned_needs") or []) for g in ticker_gaps),
        f"{prefix}_found_needs": sum(len(g.get("found_needs") or []) for g in ticker_gaps),
        f"{prefix}_missing_needs": sum(len(g.get("missing_needs") or []) for g in ticker_gaps),
    }
    if not any(g.get("missing_needs") for g in ticker_gaps):
        diagnostics["gap_mode"] = "skipped"
        if save_path is not None:
            _save(diagnostics, save_path)
        return diagnostics

    if _llm_configured(provider):
        try:
            from qwen_agent.llm import get_chat_model
            llm = get_chat_model(build_llm_cfg(model=model, provider=provider))
            response = llm.chat(
                messages=[{"role": "user", "content": _build_gap_prompt(ticker_gaps)}],
                stream=False,
                extra_generate_cfg={"temperature": 0.1},
            )
            queries = _validate_llm_queries(_parse_json(_content(response)), diagnostics)
            diagnostics["gap_mode"] = "ai"
        except Exception as exc:  # noqa: BLE001
            diagnostics["errors"].append(f"gap_analyzer_llm_failed:{type(exc).__name__}:{exc}")
            queries = _deterministic_gap_queries(ticker_gaps)
            diagnostics["gap_mode"] = "deterministic"
    else:
        queries = _deterministic_gap_queries(ticker_gaps)
        diagnostics["gap_mode"] = "deterministic"

    diagnostics["additional_search_required"] = bool(queries)
    diagnostics["total_new_queries"] = len(queries)
    diagnostics["gap_queries"] = queries
    for group in ticker_gaps:
        group["queries"] = [q for q in queries if q.get("ticker") == group.get("ticker")]
    if save_path is not None:
        _save(diagnostics, save_path)
    return diagnostics


def _deterministic_gap_queries(ticker_gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    current_year = date.today().year
    for group in ticker_gaps:
        ticker = str(group.get("ticker") or "")
        if not ticker:
            continue
        name = str(group.get("name") or ticker)
        official_domains = list(group.get("official_domains") or [])
        theme_parts = [
            str(group.get("underlying_index") or ""),
            str(group.get("theme") or ""),
            *(str(item) for item in (group.get("key_drivers") or [])),
        ]
        theme_query = next((part for part in theme_parts if part.strip()), ticker)
        for need in group.get("missing_needs") or []:
            if len(queries) >= MAX_GAP_QUERIES:
                return queries
            keywords = need.replace("_", " ")
            if need in {"theme_supply", "theme_policy", "commodity_driver", "index_rebalance", "constituent_event"}:
                query_text = f'{theme_query} "{keywords}" latest {current_year}'
                lane = "theme"
            elif need in _OFFICIAL_FIRST_NEEDS and official_domains:
                domain = str(official_domains[0]).replace("https://", "").replace("http://", "").split("/", 1)[0]
                query_text = f'site:{domain} "{name}" "{keywords}" {current_year}'
                lane = "official"
            else:
                query_text = f'"{name}" {ticker} "{keywords}" latest {current_year}'
                lane = "official_and_news"
            queries.append({
                "query": query_text,
                "lookback_days": 45,
                "language": "en",
                "reason_zh": f"合格候选未覆盖 {need}，执行官方来源优先补搜。",
                "ticker": ticker,
                "event_need": need,
                "scope": "ticker",
                "lane": lane,
            })
    return queries


def _save(diagnostics: dict[str, Any], path: "os.PathLike[str] | str") -> None:
    try:
        from pathlib import Path
        Path(path).write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
