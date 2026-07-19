# -*- coding: utf-8 -*-
"""AI Research Query Planner（修改计划第六轮第 2.1 / 2.5 / 5 / 10 / 11 / 12 节）。

Planner 复用当前 Portfolio Agent 的 provider / model / build_llm_cfg，但作为
独立调用（发生在搜索之前；Portfolio Agent 调用发生在 Evidence 形成之后）。

行为：
1. 拼装每个 Top-risk ticker 的研究上下文（风险贡献、权重、Beta、波动率、回撤、
   技术状态、工具类型、主题/底层指数、known upcoming events、上次报告事件）；
2. 调用 LLM（temperature=0.1, stream=false）让其决定 event_need / 时间窗口 /
   优先来源 / 查询关键词 / 理由；
3. Python Plan Validator 校验输出；
4. 校验失败或 LLM 不可用时降级到 deterministic fallback planner（基于 risk
   context 的规则模板），并在 plan 中标记 ``planner_mode=fallback``。

不新增第二套主模型配置；仅通过环境变量控制开关和预算。
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any

from .config import build_llm_cfg
from .research_core.language_router import determine_search_language, is_a_share
from .research_plan_schema import (
    ALLOWED_EVENT_NEEDS,
    ALLOWED_LANES,
    ALLOWED_LOOKBACK_DAYS,
    PLANNER_MAX_QUERIES_PER_QUESTION,
    PLANNER_MAX_QUESTIONS_PER_TICKER,
    PLANNER_MAX_TOTAL_QUERIES,
    PLANNER_TEMPERATURE,
    SCHEMA_DESCRIPTION_ZH,
)
from .research_plan_validator import validate_research_plan


# ── 开关 ────────────────────────────────────────────────────
def _planner_enabled() -> bool:
    return os.environ.get(
        "PORTFOLIO_RESEARCH_PLANNER_ENABLED", "true",
    ).strip().lower() in {"1", "true", "yes", "on"}


# ── LLM 响应解析 ────────────────────────────────────────────
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
    # 兼容 ```json ... ``` 包裹
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) > 2:
            s = "\n".join(lines[1:-1]).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Planner 未返回 JSON 对象")
    chunk = s[start:end + 1]
    parsed = json.loads(chunk)
    if not isinstance(parsed, dict):
        raise ValueError("Planner 输出不是对象")
    return parsed


# ── Planner 输入构建（修改计划第 5 节）─────────────────────
def _ticker_research_context(
    ticker: str,
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]],
    previous_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构建单个 ticker 的 Planner 输入上下文。"""
    instrument_metadata = instrument_metadata or {}
    meta = instrument_metadata.get(ticker, {}) or {}
    holdings = {h["ticker"]: h for h in snapshot.get("holdings", [])}
    h = holdings.get(ticker, {})
    hd = (metrics.get("holdings_detail", {}) or {}).get(ticker, {})

    ranking_items = ranking.get("items") or []
    ranking_item = next((it for it in ranking_items if it.get("ticker") == ticker), {})

    rc_map = {item.get("ticker"): item for item in metrics.get("risk_contributions", []) or []}
    rc = rc_map.get(ticker, {})

    # 风险贡献归一化（相对所有 top-risk 的风险贡献总和）
    risk_contributions = [float(it.get("risk_contribution") or 0.0) for it in metrics.get("risk_contributions", []) or []]
    total_rc = sum(risk_contributions) if risk_contributions else 0.0
    risk_contribution_ratio = (float(rc.get("risk_contribution") or 0.0) / total_rc) if total_rc > 0 else 0.0

    weights = [float(it.get("weight") or 0.0) for it in ranking_items]
    total_top_weight = sum(weights) if weights else 0.0
    top_risk_weight_share = (float(ranking_item.get("weight") or 0.0) / total_top_weight) if total_top_weight > 0 else 0.0

    # §9 修复：区分 portfolio_weight（持仓实际权重）与 top_risk_weight_share（风险榜内占比）
    portfolio_weight = float(h.get("weight") or 0.0)

    lang_decision = determine_search_language(ticker, instrument_metadata)

    # 已知 upcoming events / 上次报告事件
    prev_keys: list[str] = []
    prev_summaries: list[str] = []
    for ev in (previous_events or []):
        if str(ev.get("ticker") or "").upper() != ticker:
            continue
        if ev.get("event_key"):
            prev_keys.append(str(ev["event_key"]))
        if ev.get("event_title_zh"):
            prev_summaries.append(str(ev["event_title_zh"]))

    return {
        "ticker": ticker,
        "name": meta.get("name") or ticker,
        "instrument_type": str(meta.get("instrument_type") or "UNKNOWN").upper(),
        "market": lang_decision.get("market") or "US",
        "exchange": meta.get("exchange"),
        "search_language": lang_decision["primary_language"],
        "portfolio_weight": round(portfolio_weight, 4),
        "top_risk_weight_share": round(top_risk_weight_share, 4),
        "risk_contribution_ratio": round(risk_contribution_ratio, 4),
        "risk_priority_rank": ranking_item.get("risk_priority_rank"),
        "risk_contribution_rank": ranking_item.get("risk_contribution_rank"),
        "beta": h.get("beta"),
        "annualized_volatility_pct": hd.get("annualized_volatility"),
        "max_drawdown_63d_pct": hd.get("max_drawdown_63d"),
        "max_drawdown_252d_pct": hd.get("max_drawdown_252d"),
        "return_1m_pct": h.get("return_1m"),
        "return_ytd_pct": h.get("return_ytd"),
        "price_vs_ema20_pct": h.get("price_vs_ema20_pct"),
        "price_vs_ema50_pct": h.get("price_vs_ema50_pct"),
        "price_vs_ema200_pct": h.get("price_vs_ema200_pct"),
        "rsi": h.get("rsi"),
        "rsi_regime": h.get("rsi_regime"),
        "theme": meta.get("theme"),
        "underlying_index": meta.get("underlying_index"),
        "key_drivers": meta.get("key_drivers") or [],
        "known_upcoming_events": meta.get("known_upcoming_events") or [],
        "previous_event_keys": prev_keys[:5],
        "existing_evidence_summary": prev_summaries[:3],
        "official_domains": meta.get("official_domains") or [],
        "ir_domain": meta.get("ir_domain"),
        "sec_cik": meta.get("sec_cik"),
    }


def _build_planner_prompt(
    top_risk_tickers: list[str],
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]],
    previous_events: list[dict[str, Any]] | None,
    benchmark: str,
) -> str:
    """构建 Planner 的 user prompt。"""
    contexts = [
        _ticker_research_context(t, snapshot, metrics, ranking, instrument_metadata, previous_events)
        for t in top_risk_tickers
    ]
    payload = {
        "report_date": snapshot.get("report_date") or date.today().isoformat(),
        "benchmark": benchmark,
        "base_currency": snapshot.get("base_currency"),
        "top_risk_tickers": contexts,
        "constraints": {
            "max_questions_per_ticker": PLANNER_MAX_QUESTIONS_PER_TICKER,
            "max_queries_per_question": PLANNER_MAX_QUERIES_PER_QUESTION,
            "max_total_queries": PLANNER_MAX_TOTAL_QUERIES,
            "allowed_event_needs": sorted(ALLOWED_EVENT_NEEDS),
            "allowed_lookback_days": sorted(ALLOWED_LOOKBACK_DAYS),
            "allowed_lanes": sorted(ALLOWED_LANES),
        },
    }
    return (
        "你是投资研究查询规划器，不负责生成投资建议。\n"
        "根据输入的 Portfolio 风险数据，为每个 Top-risk 标的设计最有价值的新闻研究计划。\n\n"
        "你的目标不是搜索所有相关新闻，而是发现：\n"
        "1. 最近发生的重大事件；\n"
        "2. 能解释当前风险贡献、回撤、波动或趋势变化的事件；\n"
        "3. 即将到来的关键日期；\n"
        "4. 可能改变持仓逻辑的催化剂或风险；\n"
        "5. 对 ETF/ETC 底层主题真正重要的驱动。\n\n"
        "语言要求：\n"
        "- A 股（.SS/.SZ）优先使用中文关键词；\n"
        "- 所有非 A 股标的默认使用英文关键词（包括美股、欧股、港股、ETF、ETC、指数、Crypto）；\n"
        "- 只有英文结果预计不足或官方来源使用本地语言时，才生成本地语言补搜；\n"
        "- 不要混合中英文生成冗长 Query。\n\n"
        "硬性要求：\n"
        "- 优先官方来源和最近事件；\n"
        "- 不把报价页、产品简介、静态 Factsheet 或旧财报当作最新事件；\n"
        "- 不因为价格下跌就假设资金流出、机构减持或基本面恶化；\n"
        "- 每个研究问题必须说明为什么与当前持仓风险相关；\n"
        "- 时间窗口只能选择 7、14、30、45、120、365 天；\n"
        f"- 每个 ticker 最多 {PLANNER_MAX_QUESTIONS_PER_TICKER} 个研究问题；\n"
        f"- 每个研究问题最多 {PLANNER_MAX_QUERIES_PER_QUESTION} 条 query；\n"
        f"- 总 query 数不超过 {PLANNER_MAX_TOTAL_QUERIES}；\n"
        "- 不生成投资建议；\n"
        "- 返回严格 JSON。\n\n"
        f"{SCHEMA_DESCRIPTION_ZH}\n\n"
        "输入数据：\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


# ── LLM 调用 ────────────────────────────────────────────────
def _llm_configured(provider: str) -> bool:
    provider = (provider or "dashscope").lower()
    if provider == "dashscope":
        return bool(os.environ.get("DASHSCOPE_API_KEY"))
    if provider == "deepseek":
        return bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    if provider == "openai_compatible":
        return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("QWEN_API_KEY"))
    return False


def _call_planner_llm(
    prompt: str,
    *,
    model: str,
    provider: str,
) -> dict[str, Any]:
    """调用 LLM 生成 Plan。失败时抛异常，由上层降级到 fallback。"""
    from qwen_agent.llm import get_chat_model
    llm = get_chat_model(build_llm_cfg(model=model, provider=provider))
    response = llm.chat(
        messages=[{"role": "user", "content": prompt}],
        stream=False,
        extra_generate_cfg={"temperature": PLANNER_TEMPERATURE},
    )
    text = _content(response)
    return _parse_json(text)


# ── Deterministic Fallback Planner（修改计划第 12 节）──────
def _current_year(report_date: str | None = None) -> str:
    """返回当前年份字符串（如 '2026'），从 report_date 或今天推算。"""
    if report_date:
        try:
            return str(date.fromisoformat(str(report_date)[:10]).year)
        except (ValueError, TypeError):
            pass
    return str(date.today().year)


def _current_earnings_label(report_date: str | None = None) -> str:
    """返回当前合理财报季标签（如 'Q2 2026'），从 report_date 推算。"""
    try:
        if report_date:
            d = date.fromisoformat(str(report_date)[:10])
        else:
            d = date.today()
    except (ValueError, TypeError):
        d = date.today()
    # 财报通常在季度结束后 2-6 周发布；report_date 对应的合理季度
    # 简单规则：当前季度 + 下一季度
    q = (d.month - 1) // 3 + 1
    year = d.year
    # 如果 report_date 在季度中间，当前季度的财报可能尚未发布
    # 返回两个季度的标签供 query 使用
    # 简化：返回当前季度
    return f"Q{q} {year}"


def _fallback_lookback_for_event(event_need: str) -> int:
    """根据 event_need 推荐时间窗口。"""
    if event_need in {
        "regulatory", "litigation", "security_incident", "product_event",
        "management_change", "merger_acquisition",
    }:
        return 14
    if event_need in {
        "earnings_date", "earnings_results", "guidance",
        "credit_and_financing", "capital_raise", "major_contract",
        "analyst_revision",
    }:
        return 30
    if event_need in {"fund_flow", "aum_change", "premium_discount", "trading_volume"}:
        return 14
    if event_need in {"theme_supply", "theme_policy", "commodity_driver", "crypto_regulation"}:
        return 45
    if event_need in {"index_rebalance", "governance"}:
        return 30
    if event_need == "macro_driver":
        return 45
    return 30


def _fallback_equity_queries(ticker: str, ctx: dict[str, Any], *, report_date: str = "") -> list[dict[str, Any]]:
    """基于风险上下文为 Equity 标的生成规则化研究问题。"""
    meta_name = ctx.get("name") or ticker
    lang = ctx.get("search_language") or "en"
    drawdown_63d = float(ctx.get("max_drawdown_63d_pct") or 0.0)
    rsi_regime = str(ctx.get("rsi_regime") or "").lower()
    price_vs_ema200 = float(ctx.get("price_vs_ema200_pct") or 0.0)
    beta = float(ctx.get("beta") or 0.0)
    # §10 修复：动态财报季 + 年份
    yr = _current_year(report_date)
    earnings_q = _current_earnings_label(report_date)
    questions: list[dict[str, Any]] = []

    # 1. 财报日期 / 最新财报 / 指引（基本面驱动）
    q1_event = "earnings_date"
    if lang == "zh-CN":
        queries = [f"{meta_name} {ticker} {yr}年最新财报 业绩 指引"]
        reason = "需要确认最新/即将到来的财报日期与业绩指引，验证基本面是否支持当前估值。"
    else:
        queries = [f"{meta_name} {ticker} {earnings_q} earnings date results guidance"]
        reason = "Need to verify the latest/upcoming earnings date and guidance to test whether fundamentals support the current valuation."
    questions.append({
        "question_id": f"{ticker}_Q1",
        "event_need": q1_event,
        "reason_zh": reason,
        "lane": "official_and_news",
        "lookback_days": _fallback_lookback_for_event(q1_event),
        "queries": queries,
        "preferred_domains": ctx.get("official_domains") or ([ctx.get("ir_domain")] if ctx.get("ir_domain") else []),
        "required_entities": [meta_name],
        "exclude_terms": [],
        "priority": 1,
    })

    # 2. 深度回撤时优先查信用/融资/评级
    if drawdown_63d <= -25 or price_vs_ema200 <= -30:
        q2_event = "credit_and_financing"
        if lang == "zh-CN":
            queries = [f"{meta_name} {ticker} 信用评级 融资 资本开支 债务 {yr}"]
            reason = "深度回撤可能与融资、评级或资本开支有关，需要验证近期是否存在新的信用事件。"
        else:
            queries = [f"{meta_name} {ticker} credit rating debt financing capex {yr}"]
            reason = "Deep drawdown may be linked to financing, rating or capex; verify whether a new credit event occurred."
        questions.append({
            "question_id": f"{ticker}_Q2",
            "event_need": q2_event,
            "reason_zh": reason,
            "lane": "official_and_news",
            "lookback_days": _fallback_lookback_for_event(q2_event),
            "queries": queries,
            "preferred_domains": ctx.get("official_domains") or [],
            "required_entities": [meta_name],
            "exclude_terms": [],
            "priority": 2,
        })
    else:
        q2_event = "analyst_revision"
        if lang == "zh-CN":
            queries = [f"{meta_name} {ticker} 分析师 评级 下调 上调 最新"]
            reason = "需要确认近期是否存在分析师评级修订以解释风险贡献变化。"
        else:
            queries = [f"{meta_name} {ticker} analyst downgrade upgrade latest"]
            reason = "Need to confirm whether recent analyst revisions explain the change in risk contribution."
        questions.append({
            "question_id": f"{ticker}_Q2",
            "event_need": q2_event,
            "reason_zh": reason,
            "lane": "news",
            "lookback_days": _fallback_lookback_for_event(q2_event),
            "queries": queries,
            "preferred_domains": [],
            "required_entities": [meta_name],
            "exclude_terms": [],
            "priority": 2,
        })

    # 3. 超卖时查近期重大事件 + 监管/诉讼
    if rsi_regime == "oversold":
        q3_event = "regulatory"
        if lang == "zh-CN":
            queries = [f"{meta_name} {ticker} 监管 处罚 诉讼 重大事件 {yr}"]
            reason = "RSI 超卖状态下需要排查是否存在未识别的近期重大事件。"
        else:
            queries = [f"{meta_name} {ticker} regulatory investigation lawsuit {yr}"]
            reason = "Oversold RSI requires checking for unidentified recent material events."
        questions.append({
            "question_id": f"{ticker}_Q3",
            "event_need": q3_event,
            "reason_zh": reason,
            "lane": "news",
            "lookback_days": _fallback_lookback_for_event(q3_event),
            "queries": queries,
            "preferred_domains": [],
            "required_entities": [meta_name],
            "exclude_terms": [],
            "priority": 3,
        })

    # 4. 高 Beta 时查产品/合同/管理层
    if beta >= 1.5:
        q4_event = "product_event"
        if lang == "zh-CN":
            queries = [f"{meta_name} {ticker} 产品 重大合同 管理层变动 {yr}"]
            reason = "高 Beta 标的对公司层面事件敏感，需要确认近期产品/合同/管理层变化。"
        else:
            queries = [f"{meta_name} {ticker} product launch major contract management change {yr}"]
            reason = "High-beta name is sensitive to company-level events; verify recent product/contract/management changes."
        questions.append({
            "question_id": f"{ticker}_Q4",
            "event_need": q4_event,
            "reason_zh": reason,
            "lane": "news",
            "lookback_days": _fallback_lookback_for_event(q4_event),
            "queries": queries,
            "preferred_domains": [],
            "required_entities": [meta_name],
            "exclude_terms": [],
            "priority": 4,
        })

    return questions[:PLANNER_MAX_QUESTIONS_PER_TICKER]


def _fallback_etf_queries(ticker: str, ctx: dict[str, Any], *, report_date: str = "") -> list[dict[str, Any]]:
    """基于风险上下文为 ETF/ETC/Index 标的生成主题驱动研究问题。

    §10 修复：每个 key_driver 独立成 question，不再只用 key_drivers[0]。
    """
    meta_name = ctx.get("name") or ticker
    theme = ctx.get("theme") or ""
    underlying = ctx.get("underlying_index") or ""
    key_drivers = ctx.get("key_drivers") or []
    drawdown_63d = float(ctx.get("max_drawdown_63d_pct") or 0.0)
    yr = _current_year(report_date)
    questions: list[dict[str, Any]] = []

    # §10 修复：每个 key_driver 独立成 question
    # 第 1 批：每个 driver 一个 supply 问题
    drivers = key_drivers if key_drivers else [theme or underlying or meta_name]
    saw_flow = False
    for di, driver in enumerate(drivers[:3]):  # 最多 3 个 driver，避免超限
        q_event = "theme_supply"
        queries = [f"{driver} supply outage production latest {yr}"]
        reason = f"ETF/ETC 标的需要追溯底层主题驱动：{driver}。"
        if drawdown_63d <= -20 and di == 0:
            queries.append(f"{driver} supply disruption policy risk {yr}")
            reason += " 深度回撤提示可能存在供给侧冲击。"
        questions.append({
            "question_id": f"{ticker}_D{di + 1}",
            "event_need": q_event,
            "reason_zh": reason,
            "lane": "theme",
            "lookback_days": _fallback_lookback_for_event(q_event),
            "queries": queries,
            "preferred_domains": [],
            "required_entities": [meta_name, driver],
            "exclude_terms": [],
            "priority": di + 1,
        })
        saw_flow = True

    # 最后 1 个 slot：资金流 / AUM / 溢价折价（如果还有空间）
    remaining = PLANNER_MAX_QUESTIONS_PER_TICKER - len(questions)
    if remaining > 0:
        q3_event = "fund_flow"
        queries = [f"{ticker} ETF fund flows AUM premium discount {yr}"]
        questions.append({
            "question_id": f"{ticker}_FLOW",
            "event_need": q3_event,
            "reason_zh": "ETF 的资金流与折溢价是市场定价偏差的领先信号。",
            "lane": "news",
            "lookback_days": _fallback_lookback_for_event(q3_event),
            "queries": queries,
            "preferred_domains": [],
            "required_entities": [ticker],
            "exclude_terms": [],
            "priority": len(questions) + 1,
        })

    return questions[:PLANNER_MAX_QUESTIONS_PER_TICKER]


def _fallback_crypto_queries(ticker: str, ctx: dict[str, Any], *, report_date: str = "") -> list[dict[str, Any]]:
    """基于风险上下文为 Crypto 标的生成研究问题。"""
    meta_name = ctx.get("name") or ticker
    base = ticker.split("-")[0] if "-" in ticker else ticker
    drawdown_63d = float(ctx.get("max_drawdown_63d_pct") or 0.0)
    yr = _current_year(report_date)
    questions: list[dict[str, Any]] = []

    q1_event = "crypto_regulation"
    queries = [f"{base} regulation ETF approval enforcement {yr}"]
    reason = "加密资产对监管事件高度敏感，需确认近期监管动态。"
    if drawdown_63d <= -25:
        queries.append(f"{base} exchange volume outflow risk {yr}")
        reason += " 深度回撤需排查交易所资金外流与流动性事件。"
    questions.append({
        "question_id": f"{ticker}_Q1",
        "event_need": q1_event,
        "reason_zh": reason,
        "lane": "news",
        "lookback_days": _fallback_lookback_for_event(q1_event),
        "queries": queries,
        "preferred_domains": [],
        "required_entities": [base, meta_name],
        "exclude_terms": [],
        "priority": 1,
    })

    q2_event = "trading_volume"
    queries = [f"{base} ETF flows institutional demand {yr}"]
    questions.append({
        "question_id": f"{ticker}_Q2",
        "event_need": q2_event,
        "reason_zh": "ETF 资金流与机构需求是 Crypto 中期定价的核心驱动。",
        "lane": "news",
        "lookback_days": _fallback_lookback_for_event(q2_event),
        "queries": queries,
        "preferred_domains": [],
        "required_entities": [base],
        "exclude_terms": [],
        "priority": 2,
    })

    return questions[:PLANNER_MAX_QUESTIONS_PER_TICKER]


def _build_fallback_plan(
    top_risk_tickers: list[str],
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]],
    previous_events: list[dict[str, Any]] | None,
    benchmark: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """构建 deterministic fallback plan（修改计划第 12 节）。"""
    report_date = str(snapshot.get("report_date") or "")
    tickers_out: list[dict[str, Any]] = []
    for ticker in top_risk_tickers:
        ctx = _ticker_research_context(
            ticker, snapshot, metrics, ranking, instrument_metadata, previous_events,
        )
        itype = str(ctx.get("instrument_type") or "UNKNOWN").upper()
        if itype == "EQUITY":
            qs = _fallback_equity_queries(ticker, ctx, report_date=report_date)
        elif itype in {"ETF", "ETC", "INDEX", "FUND"}:
            qs = _fallback_etf_queries(ticker, ctx, report_date=report_date)
        elif itype == "CRYPTO":
            qs = _fallback_crypto_queries(ticker, ctx, report_date=report_date)
        else:
            qs = _fallback_equity_queries(ticker, ctx, report_date=report_date)
        if not qs:
            continue
        tickers_out.append({
            "ticker": ticker,
            "research_priority": "high" if (ctx.get("risk_contribution_ratio") or 0) >= 0.10 else "medium",
            "primary_language": ctx.get("search_language") or "en",
            "research_questions": qs,
        })

    macro_questions: list[dict[str, Any]] = [{
        "question_id": "MACRO_Q1",
        "event_need": "macro_driver",
        "reason_zh": "组合层面的宏观/系统性因素影响全部持仓的风险偏好与贴现率。",
        "lane": "macro",
        "lookback_days": 45,
        "queries": [f"{benchmark} interest rates macro market risk outlook {_current_year(report_date)}"],
        "preferred_domains": [],
        "required_entities": [],
        "exclude_terms": [],
        "priority": 1,
    }]

    return {
        "plan_version": "1.0",
        "planner_model": "fallback",
        "planner_mode": "fallback",
        "planner_fallback_reason": reason,
        "tickers": tickers_out,
        "macro_questions": macro_questions,
    }


# ── Repair / Salvage 工具（第七轮 P0-6）────────────────────

def _planner_repair_enabled() -> bool:
    """环境变量控制是否启用 Planner Repair Retry（计划 §8.2）。"""
    return os.environ.get(
        "PORTFOLIO_PLANNER_REPAIR_ENABLED", "true",
    ).strip().lower() in {"1", "true", "yes", "on"}


def _save_plan_json(path: "os.PathLike[str] | str | None", suffix: str, data: Any) -> None:
    """在 save_path 同级目录写入 *_raw / *_sanitized / *_errors JSON。"""
    if path is None:
        return
    import pathlib
    base = pathlib.Path(path)
    target = base.with_name(base.stem + suffix + base.suffix)
    try:
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _call_planner_repair(
    raw_plan: dict[str, Any],
    sanitized_plan: dict[str, Any],
    errors: list[dict[str, Any]],
    *,
    model: str,
    provider: str,
) -> dict[str, Any] | None:
    """Repair Retry：将结构化错误 + 已清洗 partial plan 反馈 LLM 修复一次（计划 §8.2）。

    返回修复后的 plan dict，失败返回 None。
    """
    if not errors:
        return None
    error_text = json.dumps(
        [{"path": e.get("path"), "code": e.get("code"), "message": e.get("message"),
          "received": e.get("received"), "allowed": e.get("allowed")}
         for e in errors],
        ensure_ascii=False, indent=2,
    )
    partial_text = json.dumps(
        {k: v for k, v in sanitized_plan.items() if k not in ("warnings", "total_queries")},
        ensure_ascii=False, indent=2,
    )

    repair_prompt = f"""你的上一个 Research Plan 输出通过了结构校验，但存在以下致命错误。请修复这些错误，保持其余内容不变。

## 致命校验错误
{error_text}

## 当前已清洗的 Partial Plan（仅合法部分保留）
{partial_text}

## 修复要求
1. 仅修正上述错误涉及的字段，不得改动已通过校验的 ticker/question；
2. 缺失的 ticker（不在 top_risk_tickers 内）需移除；
3. 对错误中缺失的字段按规范补齐；
4. 返回完整的 JSON Plan 对象。

请直接返回修复后的 JSON Plan（不要 markdown 包裹）。"""

    try:
        return _call_planner_llm(repair_prompt, model=model, provider=provider)
    except Exception:
        return None


def _salvage_hybrid_plan(
    ai_validated: dict[str, Any],
    top_risk_tickers: list[str],
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]],
    previous_events: list[dict[str, Any]] | None,
    benchmark: str,
) -> dict[str, Any]:
    """Hybrid Salvage：保留合法 AI ticker/question，缺失的用 fallback 补齐（计划 §8.3）。

    返回 hybrid plan（planner_mode="hybrid"），diagnostics 记录 ai_questions / fallback_questions。
    """
    ai_tickers: set[str] = {
        str(t["ticker"]).upper()
        for t in (ai_validated.get("tickers") or []) if isinstance(t, dict)
    }
    missing_tickers = [t for t in top_risk_tickers if t not in ai_tickers]

    fallback_tickers: list[dict[str, Any]] = []
    if missing_tickers:
        fallback_plan = _build_fallback_plan(
            missing_tickers, snapshot, metrics, ranking, instrument_metadata,
            previous_events, benchmark, reason="planner_salvage",
        )
        fallback_tickers = fallback_plan.get("tickers") or []

    merged_tickers = list(ai_validated.get("tickers") or []) + fallback_tickers
    merged_macro = list(ai_validated.get("macro_questions") or [])

    hybrid = {
        "plan_version": "1.0",
        "planner_model": ai_validated.get("planner_model") or "hybrid",
        "planner_mode": "hybrid",
        "planner_fallback_reason": "plan_validation_failed_after_repair",
        "tickers": merged_tickers,
        "macro_questions": merged_macro,
        "salvage_ai_tickers": sorted(ai_tickers),
        "salvage_fallback_tickers": missing_tickers,
        "ai_questions": sum(len(t.get("research_questions") or []) for t in (ai_validated.get("tickers") or [])),
        "fallback_questions": sum(len(t.get("research_questions") or []) for t in fallback_tickers),
    }
    return hybrid


# ── 主入口 ──────────────────────────────────────────────────
def build_ai_research_plan(
    *,
    top_risk_tickers: list[str],
    snapshot: dict[str, Any],
    metrics: dict[str, Any],
    ranking: dict[str, Any],
    instrument_metadata: dict[str, dict[str, Any]],
    previous_events: list[dict[str, Any]] | None = None,
    model: str,
    provider: str,
    benchmark: str = "^GSPC",
    save_path: "os.PathLike[str] | str | None" = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """构建并校验 AI Research Plan。

    返回 (validated_plan, diagnostics)：
    - validated_plan：校验后的 plan（``planner_mode`` ∈ ``{ai, fallback}``）。
    - diagnostics：包含 planner_mode / model / provider / errors / fallback_reason 等。

    Planner 复用 Portfolio Agent 的 provider / model / build_llm_cfg，但作为
    独立调用（修改计划 2.5）。
    """
    diagnostics: dict[str, Any] = {
        "planner_enabled": _planner_enabled(),
        "planner_model": model,
        "planner_provider": provider,
        "planner_mode": None,
        "planner_errors": [],
        "planner_fallback_reason": None,
        "planner_temperature": PLANNER_TEMPERATURE,
    }

    if not top_risk_tickers:
        diagnostics["planner_mode"] = "fallback"
        diagnostics["planner_fallback_reason"] = "no_top_risk_tickers"
        plan = _build_fallback_plan(
            [], snapshot, metrics, ranking, instrument_metadata,
            previous_events, benchmark, reason="no_top_risk_tickers",
        )
        return plan, diagnostics

    # Fallback 1：环境变量关闭 Planner
    if not _planner_enabled():
        diagnostics["planner_mode"] = "fallback"
        diagnostics["planner_fallback_reason"] = "planner_disabled_by_env"
        plan = _build_fallback_plan(
            top_risk_tickers, snapshot, metrics, ranking, instrument_metadata,
            previous_events, benchmark, reason="planner_disabled_by_env",
        )
        validated, errors = validate_research_plan(
            plan,
            snapshot=snapshot, metrics=metrics, ranking=ranking,
            instrument_metadata=instrument_metadata,
            top_risk_tickers=top_risk_tickers,
        )
        diagnostics["planner_errors"] = errors
        if save_path is not None:
            try:
                import pathlib
                pathlib.Path(save_path).write_text(
                    json.dumps(validated, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                diagnostics["planner_errors"].append(f"save_plan_failed: {exc}")
        return validated, diagnostics

    # Fallback 2：LLM 未配置
    if not _llm_configured(provider):
        diagnostics["planner_mode"] = "fallback"
        diagnostics["planner_fallback_reason"] = f"llm_not_configured: {provider}"
        plan = _build_fallback_plan(
            top_risk_tickers, snapshot, metrics, ranking, instrument_metadata,
            previous_events, benchmark, reason=f"llm_not_configured: {provider}",
        )
        validated, errors = validate_research_plan(
            plan,
            snapshot=snapshot, metrics=metrics, ranking=ranking,
            instrument_metadata=instrument_metadata,
            top_risk_tickers=top_risk_tickers,
        )
        diagnostics["planner_errors"] = errors
        if save_path is not None:
            try:
                import pathlib
                pathlib.Path(save_path).write_text(
                    json.dumps(validated, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                diagnostics["planner_errors"].append(f"save_plan_failed: {exc}")
        return validated, diagnostics

    # 主路径：调用 LLM（第七轮 P0-6：保存 raw，Repair Retry，Salvage hybrid）
    prompt = _build_planner_prompt(
        top_risk_tickers, snapshot, metrics, ranking, instrument_metadata,
        previous_events, benchmark,
    )
    raw_plan: dict[str, Any] | None = None
    llm_error: str | None = None
    try:
        raw_plan = _call_planner_llm(prompt, model=model, provider=provider)
    except Exception as exc:  # noqa: BLE001
        llm_error = f"{type(exc).__name__}: {exc}"
        diagnostics["planner_errors"].append(
            {"path": "llm_call", "code": "llm_call_failed", "message": llm_error, "fatal": True}
        )

    if raw_plan is not None:
        raw_plan.setdefault("planner_model", model)

        # 7.1 保存原始 plan（计划 §8）
        _save_plan_json(save_path, "_raw", raw_plan)

        validated, errors = validate_research_plan(
            raw_plan,
            snapshot=snapshot, metrics=metrics, ranking=ranking,
            instrument_metadata=instrument_metadata,
            top_risk_tickers=top_risk_tickers,
        )

        if not errors:
            # 校验通过 → ai mode
            validated["planner_mode"] = "ai"
            validated["planner_model"] = model
            diagnostics["planner_mode"] = "ai"
            diagnostics["planner_errors"] = []
            _save_plan_json(save_path, "_sanitized", validated)
            if save_path is not None:
                try:
                    import pathlib
                    pathlib.Path(save_path).write_text(
                        json.dumps(validated, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                except Exception as exc:  # noqa: BLE001
                    diagnostics["planner_errors"].append(
                        {"path": "save_plan", "code": "save_failed", "message": str(exc), "fatal": False}
                    )
            return validated, diagnostics

        # 校验有致命错误 — 保存错误 JSON（计划 §8）
        diagnostics["planner_errors"] = errors
        _save_plan_json(save_path, "_errors", errors)

        # 7.2 Repair Retry（计划 §8.2）
        if _planner_repair_enabled():
            repaired = _call_planner_repair(
                raw_plan, validated, errors,
                model=model, provider=provider,
            )
            if repaired is not None:
                repaired.setdefault("planner_model", model)
                validated2, errors2 = validate_research_plan(
                    repaired,
                    snapshot=snapshot, metrics=metrics, ranking=ranking,
                    instrument_metadata=instrument_metadata,
                    top_risk_tickers=top_risk_tickers,
                )
                if not errors2:
                    validated2["planner_mode"] = "ai"
                    validated2["planner_model"] = model
                    diagnostics["planner_mode"] = "ai"
                    diagnostics["planner_errors"] = []
                    diagnostics["planner_repair_succeeded"] = True
                    _save_plan_json(save_path, "_sanitized", validated2)
                    if save_path is not None:
                        try:
                            import pathlib
                            pathlib.Path(save_path).write_text(
                                json.dumps(validated2, ensure_ascii=False, indent=2, default=str),
                                encoding="utf-8",
                            )
                        except Exception as exc:  # noqa: BLE001
                            diagnostics["planner_errors"].append(
                                {"path": "save_plan", "code": "save_failed", "message": str(exc), "fatal": False}
                            )
                    return validated2, diagnostics
                # repair 仍失败 → 记录，继续 salvage
                diagnostics["planner_repair_succeeded"] = False
                validated, errors = validated2, errors2

        # 7.3 Hybrid Salvage（计划 §8.3）
        diagnostics["planner_mode"] = "hybrid"
        diagnostics["planner_fallback_reason"] = "plan_validation_failed_after_repair"
        hybrid_plan = _salvage_hybrid_plan(
            validated, top_risk_tickers, snapshot, metrics, ranking,
            instrument_metadata, previous_events, benchmark,
        )
        validated_final, final_errors = validate_research_plan(
            hybrid_plan,
            snapshot=snapshot, metrics=metrics, ranking=ranking,
            instrument_metadata=instrument_metadata,
            top_risk_tickers=top_risk_tickers,
        )
        diagnostics["planner_errors"].extend(final_errors)
        diagnostics["salvage_ai_tickers"] = hybrid_plan.get("salvage_ai_tickers") or []
        diagnostics["salvage_fallback_tickers"] = hybrid_plan.get("salvage_fallback_tickers") or []
        diagnostics["ai_questions"] = hybrid_plan.get("ai_questions") or 0
        diagnostics["fallback_questions"] = hybrid_plan.get("fallback_questions") or 0

        _save_plan_json(save_path, "_sanitized", validated_final)
        if save_path is not None:
            try:
                import pathlib
                pathlib.Path(save_path).write_text(
                    json.dumps(validated_final, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                diagnostics["planner_errors"].append(
                    {"path": "save_plan", "code": "save_failed", "message": str(exc), "fatal": False}
                )
        return validated_final, diagnostics

    # LLM 调用完全失败 → fallback
    diagnostics["planner_fallback_reason"] = f"llm_call_failed: {llm_error}"

    # 降级（保持与旧逻辑兼容）
    diagnostics["planner_mode"] = "fallback"
    plan = _build_fallback_plan(
        top_risk_tickers, snapshot, metrics, ranking, instrument_metadata,
        previous_events, benchmark,
        reason=diagnostics["planner_fallback_reason"] or "unknown",
    )
    validated, errors = validate_research_plan(
        plan,
        snapshot=snapshot, metrics=metrics, ranking=ranking,
        instrument_metadata=instrument_metadata,
        top_risk_tickers=top_risk_tickers,
    )
    diagnostics["planner_errors"].extend(errors)
    if save_path is not None:
        try:
            import pathlib
            pathlib.Path(save_path).write_text(
                json.dumps(validated, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            diagnostics["planner_errors"].append(
                {"path": "save_plan", "code": "save_failed", "message": str(exc), "fatal": False}
            )
    return validated, diagnostics
