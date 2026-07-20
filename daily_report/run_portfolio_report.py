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
    make_evidence_uid,
    split_evidence_groups,
    validate_evidence_identity,
)
from portfolio_analysis.return_model import build_portfolio_return_model
from portfolio_analysis.research_diagnostics import (
    merge_evidence_by_identity,
    refresh_research_stage_diagnostics,
)
from portfolio_analysis.observation_view import _build_observation_view
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
    """修改计划第六轮第 22 节：Action Evidence Gate + P0-6 完整 Observation 展示。

    - 无 Material Evidence 的 directional action → 完整重写全部展示字段；
    - raw_ai_* 保留原始 AI 输出供 debug。
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
                f"为何暂不执行：当前缺少符合 Materiality 与主体匹配要求的事件证据。"
            )
            action["risk_narrative"] = None
            action["portfolio_reason"] = "该标的风险贡献显著高于其权重，列入重点观察。"
            action["technical_reason"] = "当前技术指标仅用于识别风险，不构成交易触发。"
            action["news_reason"] = "本轮没有通过研究质量门槛的事件证据。"
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
            action["validation_note"] = "无 material evidence 支撑，已转为观察项。"
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
    verified_count = sum(1 for e in acc if e.get("article_fetch_ok"))
    tier12_count = sum(1 for e in acc if str(e.get("source_quality") or "").startswith(("tier_1", "tier_2")))
    event_count = len({str(e.get("event_key") or e.get("evidence_uid")) for e in acc if e.get("event_key")})
    latest_date = max((str(e.get("published_date") or "") for e in acc if e.get("published_date")), default=None)

    diag["accepted_top_risk_coverage"] = round(coverage, 3)
    diag["accepted_risk_weighted_coverage"] = round(rwc, 3)
    diag["accepted_fresh_count"] = fresh_count
    diag["accepted_verified_body_count"] = verified_count
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


def _normalize_legacy_research_evidence(evidence: list[dict]) -> list[dict]:
    """兼容仅实现 ``research()`` 的旧服务/测试注入。

    只有显式结构化、近期且具备事实文本的旧 Evidence 才被标记为 accepted；
    新 ``research_plan`` 管线不经过此兼容层。
    """
    for item in evidence or []:
        recency = str(item.get("recency_tier") or "")
        source_quality = str(item.get("source_quality") or "tier_3")
        has_facts = bool(item.get("facts") or item.get("summary_zh") or item.get("summary"))
        eligible = recency in {"fresh_event", "recent_background"} and source_quality != "tier_3" and has_facts
        item.setdefault("materiality_accepted", eligible)
        item.setdefault("entity_role", "primary")
        item.setdefault("is_quote_page", False)
        item.setdefault("is_reference_page", False)
        item.setdefault("chronology_conflict", False)
        item.setdefault("snippet_fallback_ok", bool(eligible and not item.get("article_fetch_ok")))
        item.setdefault("accept", bool(eligible))
    return evidence


def _ensure_evidence_uids(evidence: list[dict]) -> list[dict]:
    """为旧式/测试 Research Service 返回的 Evidence 补充稳定 UID。"""
    seen: set[str] = set()
    for index, item in enumerate(evidence or []):
        uid = str(item.get("evidence_uid") or "")
        if not uid:
            uid = make_evidence_uid(item)
        if uid in seen:
            uid = make_evidence_uid({
                **item,
                "title": f"{item.get('title') or item.get('raw_title') or ''}#{index}",
            })
        item["evidence_uid"] = uid
        seen.add(uid)
    return evidence


def _merge_evidence_by_uid(*groups: list[dict]) -> list[dict]:
    """Backward-compatible wrapper for canonical cross-pass Evidence merging."""
    return merge_evidence_by_identity(*groups)


def _refresh_research_stage_diagnostics(
    research_result: dict,
    evidence: list[dict],
) -> dict[str, object]:
    """Backward-compatible wrapper for closed final-stage diagnostics."""
    return refresh_research_stage_diagnostics(research_result, evidence)


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
    run_started_at = dt.datetime.now().astimezone().isoformat()
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

    # instrument-aware 新闻研究（第六轮：AI Research Query Planner）
    fallback_reason = ""
    evidence: list[dict] = []
    research_result: dict = {"status": "unknown", "evidence": [], "diagnostics": {}}
    research_plan: dict = {}
    planner_diagnostics: dict = {}
    legacy_research_mode = False
    svc = None
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
            legacy_research_mode = True
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

    snapshot["run_timeline"]["news_search_completed_at"] = (
        (research_result.get("diagnostics") or {}).get("news_search_executed_at")
        or dt.datetime.now().astimezone().isoformat()
    )

    if legacy_research_mode:
        # Legacy/injected research services already return structured evidence.
        # Do not send test fixtures or caller-owned evidence through an external
        # LLM: doing so makes offline runs non-deterministic and may change the
        # explicit acceptance decision supplied by the caller.
        evidence = _normalize_legacy_research_evidence(evidence)
    evidence = _ensure_evidence_uids(evidence)
    if legacy_research_mode:
        summary_result = {
            "status": "legacy_structured",
            "evidence": evidence,
            "summarized_count": 0,
            "accepted_count": sum(1 for item in evidence if item.get("accept") is True),
            "rejected_count": sum(1 for item in evidence if item.get("accept") is not True),
            "errors": [],
        }
        evidence = list(summary_result.get("evidence") or evidence)
    else:
        # Materiality-rejected candidates are retained for diagnostics but never
        # sent to the LLM.  Summarizing only eligible candidates reduces token use
        # and prevents already-rejected rows from crowding valid UIDs out of the
        # model response.
        summarizable_evidence = [
            item for item in evidence if item.get("materiality_accepted")
        ]
        summary_result = summarize_evidence_zh(
            summarizable_evidence, instrument_metadata, model=model, provider=provider,
            report_date=str(snapshot.get("report_date") or ""),
        )
        evidence = _merge_evidence_by_uid(
            evidence,
            list(summary_result.get("evidence") or summarizable_evidence),
        )

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
    research_result["diagnostics"]["summarizer_input_count"] = (
        len(evidence) if legacy_research_mode else len(summarizable_evidence)
    )
    research_result["diagnostics"]["summarizer_errors"] = summary_result.get("errors") or []
    research_result["diagnostics"]["summarizer_isolation_count"] = int(summary_result.get("isolated_count") or 0)
    research_result["diagnostics"]["summarizer_isolation_reasons"] = summary_result.get("isolation_reasons") or {}
    research_result["diagnostics"]["summarizer_isolated_by_ticker"] = summary_result.get("isolated_by_ticker") or {}
    research_result["diagnostics"]["summarizer_isolated_items"] = summary_result.get("isolated_items") or []
    research_result["diagnostics"]["accepted_evidence_count"] = len(accepted_evidence)
    research_result["diagnostics"]["rejected_evidence_count"] = len(rejected_evidence)
    research_result["diagnostics"]["reference_evidence_count"] = len(reference_evidence)
    research_result["diagnostics"]["identity_errors"] = validate_evidence_identity(evidence)
    _refresh_research_stage_diagnostics(research_result, evidence)
    if evidence:
        news_dates = [str(x.get("published_date")) for x in evidence if x.get("published_date")]
        snapshot["data_cutoffs"]["news"] = max(news_dates) if news_dates else snapshot.get("as_of")

    # P0-2: 收口后基于 accepted_evidence 重算覆盖率和新鲜度
    _recompute_accepted_coverage(research_result, ranking, metrics, accepted_evidence)

    # Round 9：Accepted Evidence 收口后的最后一次精准补搜。
    min_news_coverage = float(os.environ.get("PORTFOLIO_REPORT_MIN_NEWS_COVERAGE", "0.60"))
    current_coverage = float((research_result.get("diagnostics") or {}).get("accepted_top_risk_coverage") or 0.0)
    final_gap_result: dict = {}
    if (
        current_coverage < min_news_coverage
        and research_plan
        and svc is not None
        and hasattr(svc, "precision_gap_search")
    ):
        try:
            final_gap_result = svc.precision_gap_search(
                plan=research_plan, accepted_evidence=accepted_evidence,
                instrument_metadata=instrument_metadata, ranking=ranking, metrics=metrics,
                model=model, provider=provider,
            )
            new_candidates = list(final_gap_result.get("evidence") or [])
            if new_candidates:
                summarizable_new = [
                    item for item in new_candidates if item.get("materiality_accepted")
                ]
                final_summary = summarize_evidence_zh(
                    summarizable_new, instrument_metadata, model=model, provider=provider,
                    report_date=str(snapshot.get("report_date") or ""),
                )
                summarized_new = list(final_summary.get("evidence") or summarizable_new)
                evidence = _merge_evidence_by_uid(evidence, new_candidates, summarized_new)
                finalize_evidence_ids(evidence)
                groups = split_evidence_groups(evidence)
                accepted_evidence = groups["accepted"]
                rejected_evidence = groups["diagnostic_rejected"]
                reference_evidence = groups["reference"]
                research_result["evidence"] = evidence
                research_result["accepted_evidence"] = accepted_evidence
                research_result["rejected_evidence"] = rejected_evidence
                research_result["reference_evidence"] = reference_evidence
                research_result["raw_results"] = list(research_result.get("raw_results") or []) + list(final_gap_result.get("raw_results") or [])
                research_result["filtered_results"] = merge_evidence_by_identity(
                    list(research_result.get("filtered_results") or []),
                    list(final_gap_result.get("filtered_results") or []),
                )
                research_result["diagnostics"]["raw_results_count"] = len(research_result["raw_results"])
                research_result["diagnostics"]["filtered_results_count"] = len(research_result["filtered_results"])
                research_result["diagnostics"]["summarizer_errors"] = list(
                    research_result["diagnostics"].get("summarizer_errors") or []
                ) + list(final_summary.get("errors") or [])
                research_result["diagnostics"]["accepted_evidence_count"] = len(accepted_evidence)
                research_result["diagnostics"]["rejected_evidence_count"] = len(rejected_evidence)
                research_result["diagnostics"]["reference_evidence_count"] = len(reference_evidence)
                research_result["diagnostics"]["identity_errors"] = validate_evidence_identity(evidence)
                _refresh_research_stage_diagnostics(research_result, evidence)
                _recompute_accepted_coverage(research_result, ranking, metrics, accepted_evidence)
            research_result["diagnostics"]["post_accepted_gap"] = final_gap_result.get("diagnostics") or {}
            research_result["diagnostics"]["post_accepted_gap_performed"] = True
        except Exception as exc:  # noqa: BLE001
            research_result["diagnostics"]["post_accepted_gap_performed"] = True
            research_result["diagnostics"]["post_accepted_gap_error"] = f"{type(exc).__name__}: {exc}"
    else:
        research_result["diagnostics"]["post_accepted_gap_performed"] = False

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
    # Invalid model output is different from an unavailable provider: strict validation has
    # already proved that the AI narrative/action payload is unsafe to publish.  Default to
    # a deterministic observation report instead of aborting the whole report.  Operators
    # can opt back into fail-hard behaviour for debugging.
    allow_output_validation_fallback = os.environ.get(
        "PORTFOLIO_REPORT_ALLOW_OUTPUT_VALIDATION_FALLBACK", "true"
    ).strip().lower() in {"1", "true", "yes"}
    runner = agent_runner if agent_runner is not None else run_portfolio_agent

    if not accepted_evidence:
        # There is nothing the model may cite for event/fundamental claims.  Calling it here
        # wastes tokens and commonly produces exactly the unsupported Top5/overlap narratives
        # that the strict validator is designed to reject.  Build the observation report
        # deterministically and retain the rejected candidates only in diagnostics.
        fallback_reason = (
            "本轮研究未产生 Accepted Evidence；已跳过 AI 交易建议，"
            "生成确定性的量化观察报告。"
        )
        print(f"[WARN] {fallback_reason}", flush=True)
        advice = default_fallback_advice(snapshot, metrics, ranking, reason=fallback_reason)
    else:
        try:
            advice = runner(ctx, model=model, provider=provider, verbose=verbose)
            advice["report_mode"] = "ai"
        except PortfolioAgentOutputError as exc:
            if not (allow_quant_fallback or allow_output_validation_fallback):
                raise
            # Keep the exact rejected model payload/error in diagnostics and logs, not in the
            # user-facing report.  Invalid phrases (for example a wrong Top5 member list)
            # must not re-enter HTML through data_limitations/fallback_reason.
            diagnostics = research_result.setdefault("diagnostics", {})
            diagnostics["agent_validation_fallback"] = True
            diagnostics["agent_validation_error"] = str(exc)
            fallback_reason = (
                "AI 输出未通过严格校验，相关内容已全部忽略；"
                "本报告已安全降级为确定性量化观察。"
            )
            print(f"[WARN] AI 输出未通过严格校验：{exc}；生成安全的量化观察报告。", flush=True)
            advice = default_fallback_advice(snapshot, metrics, ranking, reason=fallback_reason)
        except PortfolioAgentUnavailable as exc:
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
    advice = apply_deterministic_action_targets(
        advice, metrics, settings, return_model=return_model,
    )

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
            "stages": gap_diag.get("gap_diagnostics") or {},
            "post_accepted": gap_diag.get("post_accepted_gap") or {},
            "post_accepted_gap_performed": gap_diag.get("post_accepted_gap_performed"),
            "post_accepted_gap_error": gap_diag.get("post_accepted_gap_error"),
        }
        try:
            paths["gap_diagnostics"].write_text(json.dumps(gap_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    snapshot["run_timeline"]["agent_completed_at"] = dt.datetime.now().astimezone().isoformat()
    snapshot["run_timeline"]["report_rendered_at"] = dt.datetime.now().astimezone().isoformat()

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
