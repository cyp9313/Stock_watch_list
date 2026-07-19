# -*- coding: utf-8 -*-
"""Portfolio 报告主流程（修改计划二次修改）。

新流程：
    snapshot -> metrics -> 风险发现 -> 工具类型元数据 -> 风险排名 ->
    instrument-aware 新闻研究 -> 结构化 Evidence Notes -> 真正调用 Portfolio AI Agent
    -> 校验（或量化降级兜底）-> 中文 HTML（与个股日报统一视觉）。

保留与 portfolio_service.py 的子进程契约：--portfolio-input / --portfolio-id /
--portfolio-name / --owner-scope / --search-provider / --run-dir / --output，
并新增可选 --model / --provider（默认读取环境变量）。仍然写出中间 JSON 便于调试与测试。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from market_data_service import MarketDataService
from portfolio_analysis import (
    build_portfolio_snapshot,
    calculate_portfolio_metrics,
    rank_portfolio_risks,
    validate_portfolio_advice,
)
from portfolio_analysis.validators import PortfolioAdviceValidationError
from portfolio_analysis.rules import generate_portfolio_rule_findings
from portfolio_analysis.instrument_metadata import build_instrument_metadata
from portfolio_analysis.snapshot import infer_ticker_currency
from portfolio_analysis.metric_contracts import (
    scan_non_finite, data_quality_score, metadata_coverage_score,
    evidence_coverage_score, evidence_freshness_score, evidence_verification_score,
)
from portfolio_analysis.action_targets import (
    apply_deterministic_action_targets, calculate_reallocation_summary,
)
from portfolio_analysis.report_quality import (
    evaluate_report_quality, PortfolioReportQualityError,
)
from daily_report.src.stock_daily_agent.research_service import ResearchService
from daily_report.src.stock_daily_agent.portfolio_research import PortfolioResearchService
from daily_report.src.stock_daily_agent.evidence_summarizer import summarize_evidence_zh
from daily_report.src.stock_daily_agent.research_core.evidence_id import (
    finalize_evidence_ids,
    split_evidence_groups,
    validate_evidence_identity,
)
from portfolio_analysis.return_model import build_portfolio_return_model
from daily_report.src.stock_daily_agent.portfolio_context import PortfolioRunContext
from daily_report.src.stock_daily_agent.portfolio_agent_runner import (
    run_portfolio_agent,
    PortfolioAgentUnavailable,
    PortfolioAgentOutputError,
)
from daily_report.src.stock_daily_agent.portfolio_schema import default_fallback_advice
from ticker_mapping import normalize_yfinance_ticker

# 在文件末尾导入 HTML 构建器，避免循环依赖。
from daily_report.scripts.build_portfolio_report import build_html  # noqa: E402
from daily_report.report_charts import (  # noqa: E402
    svg_weight_bars, svg_weight_vs_risk, svg_allocation, svg_cumulative_returns,
)


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("_") or "Portfolio"


def _latest_prices(close: pd.DataFrame) -> dict[str, float]:
    prices = {}
    for ticker in close.columns:
        series = close[ticker].dropna()
        if not series.empty:
            prices[str(ticker)] = float(series.iloc[-1])
    return prices


def _fx_rates(currencies: set[str], base_currency: str) -> dict[str, float]:
    rates = {}
    for currency in sorted(c for c in currencies if c and c != base_currency):
        source = "GBP" if currency == "GBX" else currency
        target = "GBP" if base_currency == "GBX" else base_currency
        if source == target:
            continue
        pair = f"{source}{target}=X"
        try:
            data = yf.download(pair, period="5d", interval="1d", progress=False, auto_adjust=False)
            if data is not None and not data.empty:
                close_col = "Adj Close" if "Adj Close" in data.columns else "Close"
                value = float(data[close_col].dropna().iloc[-1])
                if math.isfinite(value) and value > 0:
                    rates[f"{source}{target}"] = value
        except Exception:
            pass
    return rates


def _market_rows_from_close(close: pd.DataFrame, portfolio_page: dict) -> list[dict]:
    rows = []
    returns = close.pct_change(fill_method=None)
    for holding in portfolio_page.get("holdings", []):
        ticker = normalize_yfinance_ticker(holding.get("ticker"))
        if ticker not in close.columns:
            rows.append({"Ticker": ticker, "Name": ticker, "Currency": infer_ticker_currency(ticker, holding.get("buy_currency") or "USD")})
            continue
        series = close[ticker].dropna()
        price = float(series.iloc[-1]) if not series.empty else None
        row = {"Ticker": ticker, "Name": ticker, "Price": price, "Currency": infer_ticker_currency(ticker, holding.get("buy_currency") or "USD")}
        for name, periods in (("1D%", 1), ("5D%", 5), ("1M%", 21), ("YTD%", None)):
            try:
                if periods is None:
                    year_start = series[series.index >= pd.Timestamp(dt.date.today().replace(month=1, day=1))]
                    base = float(year_start.iloc[0]) if not year_start.empty else None
                else:
                    base = float(series.iloc[-periods - 1]) if len(series) > periods else None
                row[name] = (price / base - 1.0) * 100.0 if price and base else None
            except Exception:
                row[name] = None
        try:
            delta = series.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, pd.NA)
            row["RSI"] = float((100 - (100 / (1 + rs))).dropna().iloc[-1])
        except Exception:
            row["RSI"] = None
        for span in (20, 50, 200):
            try:
                ema = series.ewm(span=span, adjust=False).mean().iloc[-1]
                row[f"Diff_EMA{span}%"] = (price / float(ema) - 1.0) * 100.0 if price and ema else None
            except Exception:
                row[f"Diff_EMA{span}%"] = None
        rows.append(row)
    return rows


def _cumulative_returns(close: pd.DataFrame, tickers: list[str], weights: dict[str, float], benchmark: str):
    """返回 (labels, portfolio_cum, benchmark_cum) 用于累计收益图。

    §23 修复：以 benchmark 日历为准对齐 portfolio，消除 Crypto 周末与 benchmark 错位。
    """
    cols = [c for c in tickers if c in close.columns]
    if not cols:
        return [], [], []
    aligned = pd.Series({t: float(weights.get(t, 0.0)) for t in cols}).sort_index()
    aligned = aligned / aligned.sum() if aligned.sum() > 0 else aligned
    rets = close[cols].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
    port_ret = (rets[cols] * aligned).sum(axis=1, min_count=1).dropna()

    # §23: 基于 benchmark 交易日历对齐
    if benchmark in close.columns:
        bench_ret = close[benchmark].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
        # 取 benchmark 和 portfolio 的共同有效日期
        common_idx = port_ret.index.intersection(bench_ret.index)
        if len(common_idx) > 1:
            port_ret = port_ret.reindex(common_idx)
            bench_ret = bench_ret.reindex(common_idx)
        port_cum = ((1 + port_ret).cumprod() - 1).mul(100.0)
        bench_cum = ((1 + bench_ret).cumprod() - 1).mul(100.0)
    else:
        port_cum = ((1 + port_ret).cumprod() - 1).mul(100.0)
        bench_cum = pd.Series(dtype=float)

    labels = [str(d.date()) for d in port_cum.index]
    port_list = [float(v) for v in port_cum.tolist()]
    bench_list = [float(v) if pd.notna(v) else 0.0 for v in bench_cum.reindex(port_cum.index).ffill().tolist()] if not bench_cum.empty else [0.0] * len(port_list)
    return labels, port_list, bench_list


def _allocation_weights(snapshot: dict, meta: dict, key_fn) -> dict[str, float]:  # noqa: E302
    weights = {h["ticker"]: float(h.get("weight") or 0.0) for h in snapshot.get("holdings", [])}
    out: dict[str, float] = {}
    for h in snapshot.get("holdings", []):
        t = h["ticker"]
        k = key_fn(t, h)
        if k:
            out[k] = out.get(k, 0.0) + weights.get(t, 0.0)
    return out


def _apply_confidence_cap(
    advice: dict,
    snapshot: dict,
    metrics: dict,
    instrument_metadata: dict,
    evidence: list[dict],
    ranking: dict,
    non_finite: list[str],
) -> dict:
    """置信度上限：取模型、数据、覆盖、新鲜度和正文验证度的最小值。"""
    model_conf = max(0.0, min(1.0, float(advice.get("confidence", 0.5))))
    anomaly_w = (metrics.get("return_anomalies") or {}).get("anomaly_weight", 0.0) or 0.0
    dq = data_quality_score(len(non_finite), anomaly_w)
    tickers = [h["ticker"] for h in snapshot.get("holdings", [])]
    meta_cov = metadata_coverage_score(instrument_metadata, tickers)
    ev_cov = evidence_coverage_score(evidence, ranking.get("top_risk_tickers") or [])
    ev_fresh = evidence_freshness_score(evidence)
    ev_verified = evidence_verification_score(evidence)
    final = min(model_conf, dq, meta_cov, ev_cov, ev_fresh, ev_verified)
    advice["final_confidence"] = round(final, 3)
    advice["confidence_components"] = {
        "model_confidence": round(model_conf, 3),
        "data_quality": dq,
        "metadata_coverage": meta_cov,
        "evidence_coverage": ev_cov,
        "evidence_freshness": ev_fresh,
        "evidence_verification": ev_verified,
    }
    if str(advice.get("report_mode")) == "ai":
        advice["confidence"] = round(final, 3)
    evidence_by_ticker: dict[str, list[dict]] = {}
    for item in evidence:
        ticker = str(item.get("ticker") or "").upper()
        if ticker:
            evidence_by_ticker.setdefault(ticker, []).append(item)
    for action in advice.get("actions") or []:
        ticker = str(action.get("ticker") or "").upper()
        try:
            action_model = max(0.0, min(1.0, float(action.get("confidence", model_conf))))
        except (TypeError, ValueError):
            action_model = model_conf
        ticker_evidence = evidence_by_ticker.get(ticker, [])
        has_verified_fresh = any(
            item.get("recency_tier") in {"fresh_event", "recent_background"}
            and item.get("article_fetch_ok")
            for item in ticker_evidence
        )
        has_unverified_fresh = any(
            item.get("recency_tier") in {"fresh_event", "recent_background"}
            for item in ticker_evidence
        )
        ticker_evidence_score = 1.0 if has_verified_fresh else (0.6 if has_unverified_fresh else 0.3)
        action_final = min(action_model, final, dq, meta_cov, ticker_evidence_score)
        action["model_confidence"] = round(action_model, 3)
        action["final_confidence"] = round(action_final, 3)
        action["confidence"] = round(action_final, 3)
    return advice


def _guard_actions(advice: dict, evidence: list[dict], settings: dict) -> dict:
    """修改计划第六轮第 22 节：Action Evidence Gate。

    - 无 Material Evidence 的 directional action 强制转 Watch；
    - exit 缺少新鲜 thesis 证伪证据 → 降级 reduce；
    - 缺少新鲜证据的 directional action → monitor。

    §19 修复：被降级的 Action 清除 AI 原始文案，替换为确定性说明。
    原始 AI 文案保留在 raw_ai_reason / raw_ai_risk_narrative 供 debug。
    """
    fresh_by_ticker = {
        str(item.get("ticker") or "").upper()
        for item in evidence
        if item.get("ticker") and item.get("recency_tier") in {"fresh_event", "recent_background"}
    }
    material_tickers = {
        str(item.get("ticker") or "").upper()
        for item in evidence
        if item.get("ticker")
        and item.get("materiality_accepted", True)
        and item.get("entity_role") != "incidental"
        and not item.get("is_quote_page")
    }
    allow_exit = bool(settings.get("allow_exit_advice", False))
    for action in advice.get("actions") or []:
        ticker = str(action.get("ticker") or "").upper()
        current = float(action.get("current_weight") or 0.0)
        action_type = str(action.get("action") or "watch").lower()

        # 第 22.1 节：无 Material Evidence 的 directional action → watch
        if action_type in {"add", "trim", "reduce", "exit"} and ticker not in material_tickers:
            # §19 修复：保留原始 AI 文案到 debug 字段
            action["raw_ai_action"] = action_type
            action["raw_ai_reason"] = action.get("reason")
            action["raw_ai_risk_narrative"] = action.get("risk_narrative")
            action["quantitative_candidate_action"] = action_type
            action["action"] = "watch"
            action["action_timing"] = "monitor"
            action["target_weight_min"] = current
            action["target_weight_max"] = current
            action["execute_if"] = []
            action["expected_portfolio_risk_reduction"] = None
            action["expected_risk_change"] = None
            action["reason"] = (
                f"量化减仓候选，等待事件确认。\n\n"
                f"量化原因：风险贡献或回撤信号触发了方向性操作候选。\n\n"
                f"为何暂不执行：当前缺少符合 Materiality 与主体匹配要求的事件证据。\n\n"
                f"下一步：等待指定事件或价格条件确认后重新评估。"
            )
            action["risk_narrative"] = None
            action["validation_note"] = "无 material evidence 支撑，已转为观察项（量化候选保留为内部字段）。"
            continue

        if action_type == "exit" and not (allow_exit and ticker in fresh_by_ticker):
            action["raw_ai_action"] = action_type
            action["raw_ai_reason"] = action.get("reason")
            action["action"] = "reduce"
            action["validation_note"] = "退出建议缺少明确用户约束或新鲜 thesis 证伪证据，已降级为减仓。"
        if ticker not in fresh_by_ticker and action.get("action") in {"add", "trim", "reduce", "exit"}:
            action["action_timing"] = "monitor"
            action.setdefault("validation_note", "缺少该标的新鲜证据，不能作为立即交易建议。")
    return advice


def _enforce_observation_mode(advice: dict) -> dict:
    """Turn directional actions into watch items when the report is not actionable.

    §19 修复：保留原始 AI 文案到 debug 字段。
    """
    for action in advice.get("actions") or []:
        if action.get("action") in {"hold", "watch"}:
            continue
        current = float(action.get("current_weight") or 0.0)
        action["raw_ai_action"] = action.get("action")
        action["raw_ai_reason"] = action.get("reason")
        action["raw_ai_risk_narrative"] = action.get("risk_narrative")
        action["action"] = "watch"
        action["action_timing"] = "monitor"
        action["target_weight_min"] = current
        action["target_weight_max"] = current
        action["expected_portfolio_risk_reduction"] = None
        action["expected_risk_change"] = None
        action["reason"] = (
            f"报告最终置信度未达到可操作门槛，已转换为观察项。\n\n"
            f"该标的的量化信号触发了方向性操作候选，但当前证据质量不足以支撑立即执行。\n\n"
            f"下一步：等待更多材料事件确认后重新评估。"
        )
        action["risk_narrative"] = None
        action["validation_note"] = "报告最终置信度未达到可操作门槛，已转换为观察项。"
    return advice


def _data_cutoffs(close: pd.DataFrame, metadata: dict[str, dict], benchmark: str) -> dict[str, Any]:
    groups: dict[str, list[pd.Timestamp]] = {"equity": [], "etf": [], "crypto": []}
    for ticker, item in metadata.items():
        if ticker not in close.columns:
            continue
        series = close[ticker].dropna()
        if series.empty:
            continue
        itype = str(item.get("instrument_type") or "UNKNOWN").upper()
        key = "crypto" if itype == "CRYPTO" else ("etf" if itype in {"ETF", "ETC", "FUND", "INDEX"} else "equity")
        groups[key].append(pd.Timestamp(series.index[-1]))
    result = {key: str(max(values).date()) if values else None for key, values in groups.items()}
    if benchmark in close.columns and not close[benchmark].dropna().empty:
        result["benchmark"] = str(pd.Timestamp(close[benchmark].dropna().index[-1]).date())
    else:
        result["benchmark"] = None
    result["news"] = None
    return result


def run_pipeline(
    payload: dict,
    *,
    run_dir,
    output,
    portfolio_name: str,
    portfolio_id: str,
    owner_scope: str,
    model: str,
    provider: str,
    search_provider: str,
    close: "pd.DataFrame | None" = None,
    market_rows: list[dict] | None = None,
    fx_rates: dict | None = None,
    research_service=None,
    agent_runner=None,
    verbose: bool = True,
) -> dict:
    """端到端生成 Portfolio 报告。

    网络边界（行情 / 新闻 / Agent）均可注入，便于确定性测试：
    - close / market_rows / fx_rates：直接给定，跳过下载；
    - research_service：提供 .research(...) 的对象，跳过真实搜索；
    - agent_runner：提供 (ctx, model, provider, *, verbose) 的 callable，
      跳过真实 LLM。默认 run_portfolio_agent。
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    output = Path(output)

    portfolio_page = payload["portfolio_page"]
    settings = dict(portfolio_page.get("analysis_settings") or {})
    settings.setdefault("search_provider", search_provider)
    settings.setdefault("model", model)
    settings.setdefault("provider", provider)
    base_currency = settings.get("base_currency", "EUR")
    benchmark = normalize_yfinance_ticker(settings.get("benchmark") or "^GSPC")
    tickers = list(dict.fromkeys(
        normalize_yfinance_ticker(h.get("ticker"))
        for h in portfolio_page.get("holdings", [])
        if normalize_yfinance_ticker(h.get("ticker"))
    ))

    if close is None:
        # 修改计划第六轮第 30 节：下载 2y 数据，确保 252+ 有效 benchmark trading days
        close = MarketDataService.fetch_adjusted_close_batch(tickers + [benchmark], period="2y", interval="1d")
    latest_prices = _latest_prices(close)
    market_rows = payload.get("market_rows") or market_rows or _market_rows_from_close(close, portfolio_page)

    currencies = {infer_ticker_currency(t, "") for t in tickers}
    currencies.update(str(h.get("buy_currency") or "").upper() for h in portfolio_page.get("holdings", []))
    if fx_rates is None:
        fx_rates = dict(payload.get("fx_rates") or {}) or _fx_rates(currencies, base_currency)

    # 工具类型元数据（区分账户分组与行业/主题）
    enrich = os.environ.get("PORTFOLIO_ENRICH_YFINANCE", "false").strip().lower() in {"1", "true", "yes"}
    instrument_metadata = build_instrument_metadata(
        portfolio_page, market_rows=market_rows, enrich=enrich,
    )

    snapshot = build_portfolio_snapshot(
        portfolio_page,
        market_rows,
        latest_prices=latest_prices,
        fx_rates=fx_rates,
        base_currency=base_currency,
        benchmark=benchmark,
        instrument_metadata=instrument_metadata,
    )
    snapshot["analysis_settings"] = settings
    snapshot["report_date"] = dt.date.today().isoformat()
    snapshot["data_cutoffs"] = _data_cutoffs(close, instrument_metadata, benchmark)
    snapshot["as_of_prices"] = snapshot["data_cutoffs"].get("equity") or snapshot["data_cutoffs"].get("etf") or snapshot["report_date"]

    # §22 修复：统一 Return Model，所有下游模块共用（metrics / chart / scenario）
    portfolio_weights = {h["ticker"]: float(h.get("weight") or 0.0) for h in snapshot.get("holdings", [])}
    return_model = build_portfolio_return_model(close, portfolio_weights, benchmark=benchmark)
    metrics = calculate_portfolio_metrics(snapshot, close, benchmark=benchmark, return_model=return_model)
    ranking = rank_portfolio_risks(snapshot, metrics)

    # 非有限值扫描（修改计划第三轮 3）：阻断 NaN/Inf 流入报告，并降低置信度。
    non_finite = scan_non_finite({"metrics": metrics, "summary": snapshot.get("summary", {})})
    snapshot.setdefault("data_quality", {})
    snapshot["data_quality"]["non_finite_metrics"] = non_finite

    # instrument-aware 新闻研究（第六轮：AI Research Query Planner）
    fallback_reason = ""
    evidence: list[dict] = []
    research_result: dict = {"status": "unknown", "evidence": [], "diagnostics": {}}
    research_plan: dict = {}
    planner_diagnostics: dict = {}
    try:
        svc = research_service if research_service is not None else PortfolioResearchService(provider=search_provider)
        # 优先使用第六轮 research_plan 入口（AI Planner + 多通道搜索）
        if hasattr(svc, "research_plan"):
            plan_save_path = run_dir / "portfolio_research_plan.json"
            raw_research = svc.research_plan(
                top_risk_tickers=ranking.get("top_risk_tickers") or [],
                instrument_metadata=instrument_metadata,
                snapshot=snapshot,
                metrics=metrics,
                ranking=ranking,
                model=model,
                provider=provider,
                benchmark=benchmark,
                save_plan_path=plan_save_path,
            )
            if isinstance(raw_research, dict):
                research_result = raw_research
                evidence = list(raw_research.get("evidence") or [])
                research_plan = raw_research.get("research_plan") or {}
        else:
            # 向后兼容：旧版 research_service 仅实现 research()
            raw_research = svc.research(
                ranking.get("top_risk_tickers") or [],
                instrument_metadata,
                benchmark=benchmark,
            )
            if isinstance(raw_research, dict):
                research_result = raw_research
                evidence = list(raw_research.get("evidence") or [])
            else:
                evidence = list(raw_research or [])
                covered = {str(x.get("ticker")) for x in evidence if x.get("ticker")}
                top = set(ranking.get("top_risk_tickers") or [])
                coverage = len(top & covered) / len(top) if top else 1.0
                min_coverage = float(os.environ.get("PORTFOLIO_REPORT_MIN_NEWS_COVERAGE", "0.60"))
                normalized_status = "success" if evidence and coverage >= min_coverage else ("insufficient_coverage" if evidence else "no_raw_results")
                research_result = {
                    "status": normalized_status,
                    "evidence": evidence,
                    "diagnostics": {
                        "status": normalized_status,
                        "raw_results_count": len(evidence), "filtered_results_count": len(evidence),
                        "selected_evidence_count": len(evidence), "top_risk_coverage": coverage,
                    },
                    "raw_results": evidence, "filtered_results": evidence,
                }
        planner_diagnostics = {
            k: research_result.get("diagnostics", {}).get(k)
            for k in (
                "planner_mode", "planner_model", "planner_provider", "planner_enabled",
                "planner_temperature", "planner_errors", "planner_fallback_reason",
                "plan_version", "plan_total_queries", "compiled_queries_count",
                "official_lane_queries_count", "total_executed_queries",
                "risk_weighted_coverage", "news_search_executed_at",
                "latest_selected_event_date", "search_lanes",
            )
            if k in research_result.get("diagnostics", {})
        }
    except Exception as exc:  # noqa: BLE001
        fallback_reason += f" 新闻研究失败：{exc}"
        research_result = {
            "status": "provider_error", "evidence": [],
            "diagnostics": {"status": "provider_error", "errors": [f"{type(exc).__name__}: {exc}"], "top_risk_coverage": 0.0},
            "raw_results": [], "filtered_results": [],
        }

    summary_result = summarize_evidence_zh(
        evidence, instrument_metadata, model=model, provider=provider,
    )
    evidence = list(summary_result.get("evidence") or evidence)

    # 第七轮 P0 收口：统一分配 evidence_id（仅在接受 Summarizer 后、质量门前）
    finalize_evidence_ids(evidence)

    # 三组分流
    groups = split_evidence_groups(evidence)
    accepted_evidence = groups["accepted"]
    rejected_evidence = groups["diagnostic_rejected"]
    reference_evidence = groups["reference"]

    research_result["evidence"] = evidence
    research_result["accepted_evidence"] = accepted_evidence
    research_result["rejected_evidence"] = rejected_evidence
    research_result["reference_evidence"] = reference_evidence
    research_result.setdefault("diagnostics", {})["summarizer_status"] = summary_result.get("status")
    research_result["diagnostics"]["summarizer_errors"] = summary_result.get("errors") or []
    research_result["diagnostics"]["accepted_evidence_count"] = len(accepted_evidence)
    research_result["diagnostics"]["rejected_evidence_count"] = len(rejected_evidence)
    research_result["diagnostics"]["reference_evidence_count"] = len(reference_evidence)
    research_result["diagnostics"]["identity_errors"] = validate_evidence_identity(evidence)
    if evidence:
        news_dates = [str(x.get("published_date")) for x in evidence if x.get("published_date")]
        snapshot["data_cutoffs"]["news"] = max(news_dates) if news_dates else snapshot.get("as_of")

    # Top-risk news is a precondition: fail before invoking the main Portfolio Agent.
    preflight_quality = evaluate_report_quality(
        snapshot, metrics, research_result,
        {"final_confidence": 1.0, "confidence": 1.0, "actions": []}, {},
    )
    if not preflight_quality["publishable"]:
        for name, value in {
            "portfolio_report_quality.json": preflight_quality,
            "portfolio_research_diagnostics.json": research_result.get("diagnostics") or {},
            "portfolio_raw_search_results.json": research_result.get("raw_results") or [],
            "portfolio_filtered_search_results.json": research_result.get("filtered_results") or [],
            "portfolio_rejected_evidence.json": rejected_evidence,
            "portfolio_snapshot.json": snapshot,
            "portfolio_metrics.json": metrics,
        }.items():
            (run_dir / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        raise PortfolioReportQualityError(preflight_quality)

    # 真正调用 Portfolio AI Agent；不可用时生成明确标记的量化降级报告
    ctx = PortfolioRunContext(
        run_dir=run_dir,
        portfolio_name=portfolio_name,
        portfolio_id=portfolio_id,
        owner_scope=owner_scope,
        base_currency=base_currency,
        benchmark=benchmark,
        model=model,
        provider=provider,
        search_provider=search_provider,
        snapshot=snapshot,
        metrics=metrics,
        ranking=ranking,
        evidence=accepted_evidence,
        instrument_metadata=instrument_metadata,
        settings=settings,
        output_html=output,
        advice_json_path=run_dir / "portfolio_advice.json",
    )

    allow_quant_fallback = os.environ.get("PORTFOLIO_REPORT_ALLOW_QUANT_FALLBACK", "false").strip().lower() in {"1", "true", "yes"}
    runner = agent_runner if agent_runner is not None else run_portfolio_agent
    try:
        advice = runner(ctx, model=model, provider=provider, verbose=verbose)
        advice["report_mode"] = "ai"
    except (PortfolioAgentUnavailable, PortfolioAgentOutputError) as exc:
        if not allow_quant_fallback:
            raise
        fallback_reason = f"AI Agent 未参与：{exc}"
        print(f"[WARN] {fallback_reason}；生成量化降级报告。", flush=True)
        advice = default_fallback_advice(snapshot, metrics, ranking, reason=fallback_reason)
    except Exception as exc:  # noqa: BLE001
        if not allow_quant_fallback:
            raise
        fallback_reason = f"AI Agent 异常：{exc}"
        print(f"[WARN] {fallback_reason}；生成量化降级报告。", flush=True)
        advice = default_fallback_advice(snapshot, metrics, ranking, reason=fallback_reason)

    advice = _guard_actions(advice, accepted_evidence, settings)
    advice = apply_deterministic_action_targets(advice, metrics, settings)

    # 最终 Python 校验：AI 报告走 strict，失败必须阻断，不得静默修正同一份 AI 建议。
    final_mode = "strict" if str(advice.get("report_mode")) == "ai" else "fallback"
    try:
        advice = validate_portfolio_advice(advice, snapshot, accepted_evidence, mode=final_mode)
    except PortfolioAdviceValidationError as exc:
        raise PortfolioAgentOutputError("最终 strict 校验未通过：" + "; ".join(exc.errors)) from exc

    # 置信度上限（修改计划第三轮 23）：取模型置信度与各质量因子的最小值。
    advice = _apply_confidence_cap(
        advice, snapshot, metrics, instrument_metadata, accepted_evidence, ranking, non_finite,
    )

    reallocation = calculate_reallocation_summary(advice)
    advice["portfolio_reallocation"] = reallocation
    metrics.setdefault("aggregates", {})["recommended_reduction_weight"] = reallocation["estimated_weight_reduction"]

    from portfolio_analysis.validators import validate_portfolio_claims
    hard_errors, soft_warnings = validate_portfolio_claims(advice, snapshot, metrics, accepted_evidence)
    advice.setdefault("validation_warnings", []).extend(soft_warnings)
    quality = evaluate_report_quality(
        snapshot, metrics, research_result, advice,
        {"hard_errors": hard_errors, "soft_warnings": soft_warnings},
    )
    if not quality["actionable"]:
        advice = _enforce_observation_mode(advice)
        reallocation = calculate_reallocation_summary(advice)
        advice["portfolio_reallocation"] = reallocation
        metrics.setdefault("aggregates", {})["recommended_reduction_weight"] = reallocation["estimated_weight_reduction"]
    if not quality["publishable"]:
        debug_payloads = {
            "portfolio_report_quality.json": quality,
            "portfolio_research_diagnostics.json": research_result.get("diagnostics") or {},
            "portfolio_raw_search_results.json": research_result.get("raw_results") or [],
            "portfolio_filtered_search_results.json": research_result.get("filtered_results") or [],
            "portfolio_rejected_evidence.json": rejected_evidence,
            "portfolio_snapshot.json": snapshot,
            "portfolio_metrics.json": metrics,
            "portfolio_advice.json": advice,
        }
        for name, value in debug_payloads.items():
            (run_dir / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        raise PortfolioReportQualityError(quality)

    # 图表
    weights = {h["ticker"]: float(h.get("weight") or 0.0) for h in snapshot.get("holdings", [])}
    rc_map = {item.get("ticker"): item.get("risk_contribution") for item in metrics.get("risk_contributions", [])}
    charts = {
        "weight_bars": svg_weight_bars(snapshot.get("holdings", [])),
        "weight_vs_risk": svg_weight_vs_risk(snapshot.get("holdings", []), rc_map),
        "allocation_group": svg_allocation(
            _allocation_weights(snapshot, instrument_metadata, lambda t, h: (instrument_metadata.get(t, {}) or {}).get("account_group") or h.get("group")),
            "账户分组权重分布", "#58a6ff",
        ),
        "allocation_theme": svg_allocation(
            _allocation_weights(snapshot, instrument_metadata, lambda t, h: (instrument_metadata.get(t, {}) or {}).get("theme") or (instrument_metadata.get(t, {}) or {}).get("underlying_index")),
            "主题/底层指数权重分布", "#bc8cff",
        ),
    }
    labels, portfolio_cumulative_pct, benchmark_cumulative_pct = _cumulative_returns(close, tickers, weights, benchmark)
    # §22 修复：优先使用 Return Model 的统一累计收益（包含权重覆盖归一化）
    if not return_model.cumulative_returns.empty:
        port_cum = (return_model.cumulative_returns * 100).tolist()
        dates = [str(d) for d in return_model.cumulative_returns.index]
        if labels and len(port_cum) > 0:
            # 只更新 portfolio 部分，benchmark 仍用单独计算（保持兼容）
            portfolio_cumulative_pct = port_cum
            if len(port_cum) != len(labels):
                # 日期对齐：取交集
                pass  # 保持现有标签，允许少量不匹配
    if labels:
        charts["cumulative"] = svg_cumulative_returns(labels, portfolio_cumulative_pct, benchmark_cumulative_pct)

    # 确定性风险发现（供降级或风险诊断补充展示）
    risk_findings = generate_portfolio_rule_findings(snapshot, metrics, settings, instrument_metadata=instrument_metadata)

    # 中间 JSON（保持兼容）
    paths = {
        "snapshot": run_dir / "portfolio_snapshot.json",
        "metrics": run_dir / "portfolio_metrics.json",
        "ranking": run_dir / "portfolio_risk_ranking.json",
        "evidence": run_dir / "portfolio_evidence.json",
        "advice": run_dir / "portfolio_advice.json",
        "research_diagnostics": run_dir / "portfolio_research_diagnostics.json",
        "raw_search": run_dir / "portfolio_raw_search_results.json",
        "filtered_search": run_dir / "portfolio_filtered_search_results.json",
        "quality": run_dir / "portfolio_report_quality.json",
        "research_plan": run_dir / "portfolio_research_plan.json",
        "planner_diagnostics": run_dir / "portfolio_planner_diagnostics.json",
        "gap_diagnostics": run_dir / "portfolio_gap_diagnostics.json",
    }
    for key, value in (("snapshot", snapshot), ("metrics", metrics), ("ranking", ranking), ("evidence", evidence), ("advice", advice)):
        paths[key].write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    paths["research_diagnostics"].write_text(json.dumps(research_result.get("diagnostics") or {}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    paths["raw_search"].write_text(json.dumps(research_result.get("raw_results") or [], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    paths["filtered_search"].write_text(json.dumps(research_result.get("filtered_results") or [], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    paths["quality"].write_text(json.dumps(quality, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    # 第六轮：保存 research_plan 和 planner_diagnostics（research_plan 可能已由 build_ai_research_plan 写入）
    if research_plan:
        try:
            paths["research_plan"].write_text(json.dumps(research_plan, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    if planner_diagnostics:
        paths["planner_diagnostics"].write_text(json.dumps(planner_diagnostics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    # 第六轮 Phase 4：保存 gap diagnostics
    gap_diag = research_result.get("diagnostics", {})
    if gap_diag.get("gap_mode"):
        gap_payload = {
            "gap_mode": gap_diag.get("gap_mode"),
            "additional_search_required": gap_diag.get("gap_additional_search_required"),
            "total_new_queries": gap_diag.get("gap_total_new_queries"),
            "errors": gap_diag.get("gap_errors"),
            "ticker_gaps": [],  # 从 research_result 可选提取
        }
        try:
            paths["gap_diagnostics"].write_text(json.dumps(gap_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # 中文 HTML
    html_text = build_html(
        snapshot, metrics, ranking, advice, evidence,
        instrument_metadata=instrument_metadata,
        settings=settings,
        charts=charts,
        risk_findings=risk_findings,
        cumulative_labels=labels,
        fallback_reason=fallback_reason,
        research_diagnostics=research_result.get("diagnostics") or {},
        report_quality=quality,
    )
    output.write_text(html_text, encoding="utf-8")

    print(f"Portfolio report generated: {output}")
    print(f"Report mode: {advice.get('report_mode')}")
    print(f"Top-risk tickers: {', '.join(ranking.get('top_risk_tickers') or [])}")
    print(f"Evidence count: {len(evidence)}")
    print(f"Actions: {len(advice.get('actions') or [])}")
    return advice


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portfolio-input", required=True)
    parser.add_argument("--portfolio-id", required=True)
    parser.add_argument("--portfolio-name", required=True)
    parser.add_argument("--owner-scope", required=True)
    parser.add_argument("--search-provider", default="auto")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--model", default=os.environ.get("PORTFOLIO_REPORT_MODEL") or "qwen-plus")
    parser.add_argument("--provider", default=os.environ.get("PORTFOLIO_REPORT_PROVIDER") or "dashscope")
    args = parser.parse_args()

    payload = json.loads(Path(args.portfolio_input).read_text(encoding="utf-8"))
    run_pipeline(
        payload,
        run_dir=args.run_dir,
        output=args.output,
        portfolio_name=args.portfolio_name,
        portfolio_id=args.portfolio_id,
        owner_scope=args.owner_scope,
        model=args.model,
        provider=args.provider,
        search_provider=args.search_provider,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
