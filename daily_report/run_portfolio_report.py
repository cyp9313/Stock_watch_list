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
    evidence_coverage_score, evidence_freshness_score,
)
from daily_report.src.stock_daily_agent.research_service import ResearchService
from daily_report.src.stock_daily_agent.portfolio_research import PortfolioResearchService
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
    """返回 (labels, portfolio_cum, benchmark_cum) 用于累计收益图。"""
    cols = [c for c in tickers if c in close.columns]
    if not cols:
        return [], [], []
    aligned = pd.Series({t: float(weights.get(t, 0.0)) for t in cols}).sort_index()
    aligned = aligned / aligned.sum() if aligned.sum() > 0 else aligned
    rets = close[cols].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
    port_ret = (rets[cols] * aligned).sum(axis=1, min_count=1).dropna()
    port_cum = ((1 + port_ret).cumprod() - 1).mul(100.0)
    labels = [str(d.date()) for d in port_cum.index]
    port_list = [float(v) for v in port_cum.tolist()]
    bench_list = []
    if benchmark in close.columns:
        b = close[benchmark].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
        b_cum = ((1 + b).cumprod() - 1).mul(100.0).reindex(port_cum.index).ffill()
        bench_list = [float(v) if pd.notna(v) else 0.0 for v in b_cum.tolist()]
    else:
        bench_list = [0.0] * len(port_list)
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
    """置信度上限（修改计划第三轮 23）：min(模型, 数据完整度, 元数据覆盖, 证据覆盖, 证据新鲜度)。"""
    model_conf = max(0.0, min(1.0, float(advice.get("confidence", 0.5))))
    anomaly_w = (metrics.get("return_anomalies") or {}).get("anomaly_weight", 0.0) or 0.0
    dq = data_quality_score(len(non_finite), anomaly_w)
    tickers = [h["ticker"] for h in snapshot.get("holdings", [])]
    meta_cov = metadata_coverage_score(instrument_metadata, tickers)
    ev_cov = evidence_coverage_score(evidence, ranking.get("top_risk_tickers") or [])
    ev_fresh = evidence_freshness_score(evidence)
    final = min(model_conf, dq, meta_cov, ev_cov, ev_fresh)
    advice["final_confidence"] = round(final, 3)
    advice["confidence_components"] = {
        "model_confidence": round(model_conf, 3),
        "data_quality": dq,
        "metadata_coverage": meta_cov,
        "evidence_coverage": ev_cov,
        "evidence_freshness": ev_fresh,
    }
    if str(advice.get("report_mode")) == "ai":
        advice["confidence"] = round(final, 3)
    return advice


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
        close = MarketDataService.fetch_adjusted_close_batch(tickers + [benchmark], period="1y", interval="1d")
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
    # 修改计划第三轮 40：行情/基准数据截止 = 最近一个有效交易日。
    try:
        snapshot["as_of_prices"] = str(close.index[-1].date())
    except Exception:
        snapshot["as_of_prices"] = snapshot["report_date"]
    metrics = calculate_portfolio_metrics(snapshot, close, benchmark=benchmark)
    ranking = rank_portfolio_risks(snapshot, metrics)

    # 非有限值扫描（修改计划第三轮 3）：阻断 NaN/Inf 流入报告，并降低置信度。
    non_finite = scan_non_finite({"metrics": metrics, "summary": snapshot.get("summary", {})})
    snapshot.setdefault("data_quality", {})
    snapshot["data_quality"]["non_finite_metrics"] = non_finite

    # instrument-aware 新闻研究
    fallback_reason = ""
    evidence: list[dict] = []
    try:
        svc = research_service if research_service is not None else PortfolioResearchService(provider=search_provider)
        evidence = svc.research(
            ranking.get("top_risk_tickers") or [],
            instrument_metadata,
            benchmark=benchmark,
        )
    except Exception as exc:  # noqa: BLE001
        fallback_reason += f" 新闻研究失败：{exc}"

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
        evidence=evidence,
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

    # 最终 Python 校验：AI 报告走 strict（不得静默修正）；降级报告走 fallback（修改计划第三轮 30）。
    final_mode = "strict" if str(advice.get("report_mode")) == "ai" else "fallback"
    try:
        advice = validate_portfolio_advice(advice, snapshot, evidence, mode=final_mode)
    except PortfolioAdviceValidationError as exc:
        if final_mode == "strict":
            print(f"[WARN] 最终 strict 校验未通过，降级为 fallback 模式：{'; '.join(exc.errors)}", flush=True)
            advice = validate_portfolio_advice(advice, snapshot, evidence, mode="fallback")
        else:
            raise

    # 置信度上限（修改计划第三轮 23）：取模型置信度与各质量因子的最小值。
    advice = _apply_confidence_cap(
        advice, snapshot, metrics, instrument_metadata, evidence, ranking, non_finite,
    )

    # 修改计划第三轮 20：Python 预计算「计划减仓释放权重」聚合值（需 AI 操作建议）。
    red_w = 0.0
    for a in advice.get("actions") or []:
        if str(a.get("action")) in {"reduce", "trim", "exit"}:
            cur = float(a.get("current_weight") or 0.0)
            tgt = float(a.get("target_weight_min") or cur)
            red_w += max(0.0, cur - tgt)
    metrics.setdefault("aggregates", {})["recommended_reduction_weight"] = round(red_w, 4)

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
    }
    for key, value in (("snapshot", snapshot), ("metrics", metrics), ("ranking", ranking), ("evidence", evidence), ("advice", advice)):
        paths[key].write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # 中文 HTML
    html_text = build_html(
        snapshot, metrics, ranking, advice, evidence,
        instrument_metadata=instrument_metadata,
        settings=settings,
        charts=charts,
        risk_findings=risk_findings,
        cumulative_labels=labels,
        fallback_reason=fallback_reason,
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
