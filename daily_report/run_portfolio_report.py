# -*- coding: utf-8 -*-
"""Portfolio AI v2 单次联网报告主流程。

流程：snapshot -> metrics -> risk ranking -> one DashScope built-in web-search
call with deepseek-v4-flash -> local URL/date/schema validation -> quality gate ->
HTML. Portfolio 报告不调用 Serper、Query Planner、Official Lane、Gap Search、
Evidence Summarizer 或第二个 Portfolio Agent。

保留 portfolio_service.py 的子进程契约以兼容现有 job/UI；运行时固定使用 DashScope 内置搜索。
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
from portfolio_analysis.return_model import build_portfolio_return_model
from portfolio_analysis.observation_view import _build_observation_view
from daily_report.src.stock_daily_agent.portfolio_schema import default_fallback_advice
from daily_report.src.stock_daily_agent.portfolio_single_search import (
    run_portfolio_single_search,
    PortfolioSingleSearchUnavailable,
    PortfolioSingleSearchOutputError,
)
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
    """置信度上限：取模型、数据、覆盖、新鲜度和来源验证度的最小值。"""
    model_conf = max(0.0, min(1.0, float(advice.get("confidence", 0.5))))
    anomaly_w = (metrics.get("return_anomalies") or {}).get("anomaly_weight", 0.0) or 0.0
    dq = data_quality_score(len(non_finite), anomaly_w)
    tickers = [h["ticker"] for h in snapshot.get("holdings", [])]
    meta_cov = metadata_coverage_score(instrument_metadata, tickers)
    ev_cov = evidence_coverage_score(
        evidence, ranking.get("top_risk_tickers") or [], floor=0.0,
    )
    ev_fresh = evidence_freshness_score(evidence, empty_score=0.0, floor=0.0)
    ev_verified = evidence_verification_score(evidence, empty_score=0.0, floor=0.0)
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
            and (item.get("article_fetch_ok") or item.get("source_verified"))
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
    """Directionally actionable claims require locally validated Evidence.

    Unsupported directional actions are rewritten as watch items while raw AI
    fields are retained only in debug artifacts.
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

        if action_type in {"add", "trim", "reduce", "exit"} and ticker not in material_tickers:
            # P0-6: 保存所有原始 AI 字段
            for field in ("action", "reason", "risk_narrative", "portfolio_reason",
                          "technical_reason", "news_reason", "bull_case", "bear_case",
                          "execute_if", "cancel_or_upgrade_if", "further_reduce_if",
                          "monitoring_items", "thresholds"):
                raw_val = action.get(field)
                if raw_val is not None or field == "action":
                    action[f"raw_ai_{field}"] = raw_val
            action["quantitative_candidate_action"] = action_type
            action["action"] = "watch"
            action["action_timing"] = "monitor"
            action["target_weight_min"] = current
            action["target_weight_max"] = current
            # P0-6: 完整重写展示字段
            action["reason"] = (
                f"该标的风险贡献高于其权重，因此列入重点观察。\n\n"
                f"量化原因：风险贡献或回撤信号触发了方向性操作候选。\n\n"
                f"为何暂不执行：当前缺少通过本地 URL、日期与主体校验的事件证据。"
            )
            action["risk_narrative"] = None
            action["portfolio_reason"] = "该标的风险贡献显著高于其权重，列入重点观察。"
            action["technical_reason"] = "当前技术指标仅用于识别风险，不构成交易触发。"
            action["news_reason"] = "本轮没有通过本地 URL 与日期校验的事件证据。"
            action["bull_case"] = "等待新的、可验证的正面事件信号。"
            action["bear_case"] = "等待新的、可验证的负面事件信号。"
            action["execute_if"] = []
            action["cancel_or_upgrade_if"] = []
            action["further_reduce_if"] = []
            action["monitoring_items"] = [f"{ticker} 风险贡献变化", "价格关键技术位"]
            action["thresholds"] = []
            action["expected_portfolio_risk_reduction"] = None
            action["expected_risk_change"] = None
            action["evidence_ids"] = []
            action["validation_note"] = "无通过本地校验的 Evidence 支撑，已转为观察项。"
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


# P0-2: 收口后基于 accepted_evidence 重算所有研究指标
def _recompute_accepted_coverage(
    research_result: dict,
    ranking: dict,
    metrics: dict,
    accepted_evidence: list[dict],
) -> None:
    """收口后基于 Accepted Evidence 重算覆盖率、风险加权覆盖、新鲜度等。

    写入 research_result["diagnostics"] 并覆盖旧字段。
    """
    top_risk_tickers = [str(t).upper() for t in (ranking.get("top_risk_tickers") or [])]
    acc = accepted_evidence or []
    diag = research_result.setdefault("diagnostics", {})

    # Accepted ticker 覆盖
    covered = {str(e.get("ticker") or "").upper() for e in acc if e.get("ticker")}
    top_risk_set = {t for t in top_risk_tickers}
    accepted_top_risk = top_risk_set & covered
    coverage = len(accepted_top_risk) / len(top_risk_set) if top_risk_set else 0.0

    # Accepted Risk-weighted Coverage
    rc_map = {str(item.get("ticker") or "").upper(): float(item.get("risk_contribution") or 0.0)
              for item in (metrics.get("risk_contributions") or [])}
    total_rc = sum(rc_map.get(t, 0.0) for t in top_risk_tickers)
    covered_rc = sum(rc_map.get(t, 0.0) for t in accepted_top_risk)
    rwc = (covered_rc / total_rc) if total_rc > 0 else 0.0

    # Accepted Fresh Count
    fresh_count = sum(1 for e in acc if str(e.get("recency_tier") or "") in {"fresh_event", "recent_background"})
    verified_count = sum(1 for e in acc if e.get("article_fetch_ok") or e.get("source_verified"))
    tier12_count = sum(1 for e in acc if str(e.get("source_quality") or "").startswith(("tier_1", "tier_2")))
    event_count = len({str(e.get("event_key") or e.get("evidence_uid")) for e in acc if e.get("event_key")})
    latest_date = max((str(e.get("published_date") or "") for e in acc if e.get("published_date")), default=None)

    diag["accepted_top_risk_coverage"] = round(coverage, 3)
    diag["accepted_risk_weighted_coverage"] = round(rwc, 3)
    diag["accepted_fresh_count"] = fresh_count
    diag["accepted_verified_source_count"] = verified_count
    diag["accepted_tier12_count"] = tier12_count
    diag["accepted_unique_event_count"] = event_count
    diag["latest_accepted_event_date"] = latest_date
    diag["accepted_top_risk_ticker_count"] = len(accepted_top_risk)

    # 同时更新旧字段以保持兼容（P0-2: 用 accepted 值覆盖候选值）
    diag["top_risk_coverage"] = round(coverage, 3)
    diag["risk_weighted_coverage"] = round(rwc, 3)


def _enforce_observation_mode(advice: dict) -> dict:
    """Rewrite every visible action as a deterministic watch item.

    Observation mode is a report-wide state, not merely a directional-action
    downgrade.  Existing ``hold``/``watch`` rows may still contain unsupported
    AI fundamental, liquidity or sentiment claims, so every action narrative
    must be rebuilt from safe quantitative language.
    """
    for action in advice.get("actions") or []:
        current = float(action.get("current_weight") or 0.0)
        for field in ("action", "reason", "risk_narrative", "portfolio_reason",
                      "technical_reason", "news_reason", "bull_case", "bear_case",
                      "execute_if", "cancel_or_upgrade_if", "further_reduce_if",
                      "monitoring_items", "thresholds"):
            raw_val = action.get(field)
            if raw_val is not None or field == "action":
                action[f"raw_ai_{field}"] = raw_val
        action["action"] = "watch"
        action["action_timing"] = "monitor"
        action["target_weight_min"] = current
        action["target_weight_max"] = current
        action["reason"] = "报告最终置信度未达到可操作门槛，当前仅列为观察项。"
        action["risk_narrative"] = None
        action["portfolio_reason"] = "该标的按确定性风险排序列入观察，不代表需要立即调整仓位。"
        action["technical_reason"] = "价格、回撤和风险贡献指标仅用于风险观察，不构成交易触发。"
        action["news_reason"] = "本轮没有足够的 Accepted Evidence 支撑方向性操作。"
        action["bull_case"] = "等待新的、可验证的正面事件或风险指标改善。"
        action["bear_case"] = "等待新的、可验证的负面事件或风险指标恶化。"
        action["execute_if"] = []
        action["cancel_or_upgrade_if"] = []
        action["further_reduce_if"] = []
        action["monitoring_items"] = [f"{action.get('ticker', '')} 风险贡献变化", "价格与主要均线关系"]
        action["thresholds"] = []
        action["expected_portfolio_risk_reduction"] = None
        action["expected_risk_change"] = None
        action["evidence_ids"] = []
        action["validation_note"] = "观察型报告：全部操作文本已按确定性规则重建。"
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
    close: "pd.DataFrame | None" = None,
    market_rows: list[dict] | None = None,
    fx_rates: dict | None = None,
    single_search_runner=None,
    verbose: bool = True,
) -> dict:
    """端到端生成 Portfolio 报告。

    网络边界可注入，便于确定性测试：
    - close / market_rows / fx_rates：直接给定，跳过行情下载；
    - single_search_runner：替代真实 DashScope 单次联网调用。

    Portfolio AI v2 只允许一次 DashScope 内置联网调用，不调用 Serper、
    Query Planner、Gap Search、Summarizer 或第二个 Portfolio Agent。
    """
    run_started_at = dt.datetime.now().astimezone().isoformat()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    output = Path(output)

    portfolio_page = payload["portfolio_page"]
    settings = dict(portfolio_page.get("analysis_settings") or {})
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
    snapshot["run_timeline"] = {
        "run_started_at": run_started_at,
        "snapshot_completed_at": dt.datetime.now().astimezone().isoformat(),
    }

    # §22 修复：统一 Return Model，所有下游模块共用（metrics / chart / scenario）
    portfolio_weights = {h["ticker"]: float(h.get("weight") or 0.0) for h in snapshot.get("holdings", [])}
    return_model = build_portfolio_return_model(close, portfolio_weights, benchmark=benchmark)
    metrics = calculate_portfolio_metrics(snapshot, close, benchmark=benchmark, return_model=return_model)
    ranking = rank_portfolio_risks(snapshot, metrics)

    # 非有限值扫描（修改计划第三轮 3）：阻断 NaN/Inf 流入报告，并降低置信度。
    non_finite = scan_non_finite({"metrics": metrics, "summary": snapshot.get("summary", {})})
    snapshot.setdefault("data_quality", {})
    snapshot["data_quality"]["non_finite_metrics"] = non_finite

    # Portfolio AI v2: one DashScope built-in web-search call, no external API or retry.
    fallback_reason = ""
    settings["search_provider"] = "dashscope_builtin"
    settings["research_mode"] = "dashscope_single_search"
    research_result: dict = {
        "status": "unknown", "evidence": [], "accepted_evidence": [],
        "rejected_evidence": [], "reference_evidence": [], "diagnostics": {},
        "raw_results": [], "filtered_results": [],
    }
    runner = single_search_runner or run_portfolio_single_search
    try:
        kwargs = {
            "snapshot": snapshot,
            "metrics": metrics,
            "ranking": ranking,
            "instrument_metadata": instrument_metadata,
            "model": model,
            "provider": provider,
        }
        if hasattr(runner, "run"):
            raw_research = runner.run(**kwargs)
        else:
            raw_research = runner(**kwargs)
        if not isinstance(raw_research, dict):
            raise PortfolioSingleSearchOutputError("单次联网研究返回值不是对象。")
        research_result.update(raw_research)
    except (PortfolioSingleSearchUnavailable, PortfolioSingleSearchOutputError) as exc:
        fallback_reason = f"单次 DashScope 联网研究不可用：{exc}"
        research_result = {
            "status": "provider_error",
            "advice": default_fallback_advice(snapshot, metrics, ranking, reason=fallback_reason),
            "evidence": [], "accepted_evidence": [], "rejected_evidence": [],
            "reference_evidence": [], "raw_results": [], "filtered_results": [],
            "diagnostics": {
                "status": "provider_error",
                "research_mode": "dashscope_single_search",
                "provider_used": "dashscope_builtin_search",
                "model": model,
                "search_strategy": "turbo",
                "search_call_count": 0,
                "external_search_call_count": 0,
                "model_call_count": 0,
                "retry_count": 0,
                "gap_search_count": 0,
                "max_search_calls": 1,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "news_search_executed_at": dt.datetime.now().astimezone().isoformat(),
            },
        }
    except Exception as exc:  # noqa: BLE001
        fallback_reason = f"单次 DashScope 联网研究异常：{exc}"
        research_result = {
            "status": "provider_error",
            "advice": default_fallback_advice(snapshot, metrics, ranking, reason=fallback_reason),
            "evidence": [], "accepted_evidence": [],
            "rejected_evidence": [{"reasons": ["provider_error"], "error": f"{type(exc).__name__}: {exc}"}],
            "reference_evidence": [], "raw_results": [], "filtered_results": [],
            "diagnostics": {
                "status": "provider_error",
                "research_mode": "dashscope_single_search",
                "provider_used": "dashscope_builtin_search",
                "model": model,
                "search_strategy": "turbo",
                "search_call_count": 1,
                "external_search_call_count": 0,
                "model_call_count": 1,
                "retry_count": 0,
                "gap_search_count": 0,
                "max_search_calls": 1,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "news_search_executed_at": dt.datetime.now().astimezone().isoformat(),
            },
        }

    diagnostics = research_result.setdefault("diagnostics", {})
    # Product invariants: the report must never silently perform a second search.
    diagnostics.setdefault("research_mode", "dashscope_single_search")
    diagnostics.setdefault("search_strategy", "turbo")
    diagnostics.setdefault("search_call_count", 0)
    diagnostics.setdefault("external_search_call_count", 0)
    diagnostics.setdefault("model_call_count", diagnostics.get("search_call_count", 0))
    diagnostics.setdefault("retry_count", 0)
    diagnostics.setdefault("gap_search_count", 0)
    diagnostics.setdefault("max_search_calls", 1)
    if int(diagnostics.get("search_call_count") or 0) > 1:
        raise RuntimeError("Portfolio 单次联网模式违反调用预算：search_call_count > 1")
    if int(diagnostics.get("external_search_call_count") or 0) != 0:
        raise RuntimeError("Portfolio 单次联网模式禁止外部搜索 API。")
    if int(diagnostics.get("retry_count") or 0) != 0 or int(diagnostics.get("gap_search_count") or 0) != 0:
        raise RuntimeError("Portfolio 单次联网模式禁止重试与 Gap Search。")

    accepted_evidence = list(research_result.get("accepted_evidence") or research_result.get("evidence") or [])
    rejected_evidence = list(research_result.get("rejected_evidence") or [])
    reference_evidence = list(research_result.get("reference_evidence") or [])
    evidence = accepted_evidence
    research_result["evidence"] = accepted_evidence
    research_result["accepted_evidence"] = accepted_evidence
    research_result["rejected_evidence"] = rejected_evidence
    research_result["reference_evidence"] = reference_evidence
    diagnostics["accepted_evidence_count"] = len(accepted_evidence)
    diagnostics["rejected_evidence_count"] = len(rejected_evidence)
    diagnostics["reference_evidence_count"] = len(reference_evidence)
    diagnostics["raw_results_count"] = len(research_result.get("raw_results") or [])
    diagnostics["filtered_results_count"] = len(research_result.get("filtered_results") or [])
    diagnostics["selected_evidence_count"] = int(diagnostics.get("model_evidence_count") or len(accepted_evidence) + len(rejected_evidence))
    _recompute_accepted_coverage(research_result, ranking, metrics, accepted_evidence)

    snapshot["run_timeline"]["news_search_completed_at"] = (
        diagnostics.get("news_search_executed_at") or dt.datetime.now().astimezone().isoformat()
    )
    latest_news_date = diagnostics.get("latest_accepted_event_date") or diagnostics.get("latest_selected_event_date")
    if latest_news_date:
        snapshot["data_cutoffs"]["news"] = str(latest_news_date)

    advice = research_result.get("advice")
    if not isinstance(advice, dict):
        fallback_reason = fallback_reason or "单次联网模型输出缺少可用的结构化分析。"
        advice = default_fallback_advice(snapshot, metrics, ranking, reason=fallback_reason)
    if not accepted_evidence and str(advice.get("report_mode") or "") == "ai":
        fallback_reason = "本轮单次联网研究没有通过本地 URL 与日期校验的 Evidence。"
        advice = default_fallback_advice(snapshot, metrics, ranking, reason=fallback_reason)

    advice = _guard_actions(advice, accepted_evidence, settings)
    advice = apply_deterministic_action_targets(
        advice, metrics, settings, return_model=return_model,
    )

    # 单次调用模式不允许让模型重试。AI 结构校验失败时立即安全降级。
    final_mode = "strict" if str(advice.get("report_mode")) == "ai" else "fallback"
    try:
        advice = validate_portfolio_advice(advice, snapshot, accepted_evidence, mode=final_mode)
    except PortfolioAdviceValidationError as exc:
        diagnostics["model_advice_validation_error"] = "; ".join(exc.errors)
        fallback_reason = "单次联网模型分析未通过本地结构校验；未重试，已降级为量化观察报告。"
        advice = default_fallback_advice(snapshot, metrics, ranking, reason=fallback_reason)
        advice = apply_deterministic_action_targets(advice, metrics, settings, return_model=return_model)
        advice = validate_portfolio_advice(advice, snapshot, accepted_evidence, mode="fallback")

    # 置信度上限（修改计划第三轮 23）：取模型置信度与各质量因子的最小值。
    advice = _apply_confidence_cap(
        advice, snapshot, metrics, instrument_metadata, accepted_evidence, ranking, non_finite,
    )

    reallocation = calculate_reallocation_summary(advice)
    advice["portfolio_reallocation"] = reallocation
    metrics.setdefault("aggregates", {})["recommended_reduction_weight"] = reallocation["estimated_weight_reduction"]

    # 先判定是否具备可操作条件；不可操作时在 Claim Validation 前清除原始 AI 叙事。
    preliminary_quality = evaluate_report_quality(snapshot, metrics, research_result, advice, {})
    if not preliminary_quality["actionable"]:
        advice = _enforce_observation_mode(advice)
        advice = _build_observation_view(advice, snapshot, metrics, ranking, accepted_evidence)
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
    if not quality["publishable"]:
        debug_payloads = {
            "portfolio_report_quality.json": quality,
            "portfolio_research_diagnostics.json": research_result.get("diagnostics") or {},
            "portfolio_raw_search_results.json": research_result.get("raw_results") or [],
            "portfolio_filtered_search_results.json": research_result.get("filtered_results") or [],
            "portfolio_rejected_evidence.json": rejected_evidence,
            "portfolio_reference_evidence.json": reference_evidence,
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
    # P0-8: 优先使用 Return Model 统一日期索引（Portfolio + Benchmark 对齐）
    if not return_model.cumulative_returns.empty:
        port_cum = (return_model.cumulative_returns * 100).tolist()
        bench_cum = (return_model.benchmark_cumulative_returns * 100).tolist() if not return_model.benchmark_cumulative_returns.empty else []
        dates = [str(d) for d in return_model.cumulative_returns.index]
        if len(dates) > 0:
            labels = dates
            portfolio_cumulative_pct = port_cum
            if bench_cum and len(bench_cum) == len(dates):
                benchmark_cumulative_pct = bench_cum
    if labels:
        charts["cumulative"] = svg_cumulative_returns(labels, portfolio_cumulative_pct, benchmark_cumulative_pct)

    # 确定性风险发现（供降级或风险诊断补充展示）
    risk_findings = generate_portfolio_rule_findings(snapshot, metrics, settings, instrument_metadata=instrument_metadata)

    # 在写入工件前固定最终时间线，确保 JSON 与 HTML 口径一致。
    snapshot["run_timeline"]["single_search_completed_at"] = (
        diagnostics.get("news_search_executed_at") or dt.datetime.now().astimezone().isoformat()
    )
    snapshot["run_timeline"]["report_rendered_at"] = dt.datetime.now().astimezone().isoformat()

    # 中间 JSON：只保留单次联网模式实际产生的工件。
    paths = {
        "snapshot": run_dir / "portfolio_snapshot.json",
        "metrics": run_dir / "portfolio_metrics.json",
        "ranking": run_dir / "portfolio_risk_ranking.json",
        "evidence": run_dir / "portfolio_evidence.json",
        "rejected_evidence": run_dir / "portfolio_rejected_evidence.json",
        "reference_evidence": run_dir / "portfolio_reference_evidence.json",
        "advice": run_dir / "portfolio_advice.json",
        "research_diagnostics": run_dir / "portfolio_research_diagnostics.json",
        "dashscope_sources": run_dir / "portfolio_dashscope_sources.json",
        "raw_model_output": run_dir / "portfolio_single_search_raw_output.txt",
        "raw_model_payload": run_dir / "portfolio_single_search_raw_payload.json",
        "quality": run_dir / "portfolio_report_quality.json",
    }
    json_payloads = {
        "snapshot": snapshot, "metrics": metrics, "ranking": ranking,
        "evidence": evidence, "rejected_evidence": rejected_evidence,
        "reference_evidence": reference_evidence,
        "advice": advice, "research_diagnostics": diagnostics,
        "dashscope_sources": research_result.get("sources") or research_result.get("raw_results") or [],
        "raw_model_payload": research_result.get("raw_model_payload") or {},
        "quality": quality,
    }
    for key, value in json_payloads.items():
        paths[key].write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    paths["raw_model_output"].write_text(str(research_result.get("raw_model_output") or ""), encoding="utf-8")

    # 中文 HTML（仅传入 accepted_evidence）
    html_text = build_html(
        snapshot, metrics, ranking, advice, accepted_evidence,
        instrument_metadata=instrument_metadata,
        settings=settings,
        charts=charts,
        risk_findings=risk_findings,
        cumulative_labels=labels,
        fallback_reason=fallback_reason,
        research_diagnostics=research_result.get("diagnostics") or {},
        report_quality=quality,
        rejected_evidence=rejected_evidence,
        reference_evidence=reference_evidence,
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
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--model", default=os.environ.get("PORTFOLIO_REPORT_MODEL") or "deepseek-v4-flash")
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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
