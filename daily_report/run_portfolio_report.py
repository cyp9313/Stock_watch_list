# -*- coding: utf-8 -*-
"""Portfolio AI Analyst v3 report pipeline.

Python computes immutable portfolio metrics. One DeepSeek-v4-Pro DashScope call
adds integrated technical/news/risk interpretation according to user settings.
The legacy evidence-gate pipeline is no longer used by the production path.
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
)
from portfolio_analysis.rules import generate_portfolio_rule_findings
from portfolio_analysis.instrument_metadata import build_instrument_metadata
from portfolio_analysis.snapshot import infer_ticker_currency
from portfolio_analysis.metric_contracts import scan_non_finite
from portfolio_analysis.action_targets import apply_deterministic_action_targets
from portfolio_analysis.return_model import build_portfolio_return_model
from daily_report.src.stock_daily_agent.portfolio_fallback import build_quantitative_fallback
from daily_report.src.stock_daily_agent.portfolio_ai_analyst import (
    run_portfolio_ai_analyst,
    normalize_analyst_settings,
    PortfolioAnalystUnavailable,
    PortfolioAnalystOutputError,
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


def _search_sources_as_notes(sources: list[dict], existing_urls: set[str]) -> list[dict]:
    """Expose DashScope-returned sources without turning them into trading evidence."""
    items = []
    for index, source in enumerate(sources or []):
        url = str(source.get("url") or "").strip()
        if not url or url in existing_urls:
            continue
        items.append({
            "reference_id": f"S{index + 1:03d}",
            "ticker": None,
            "title": str(source.get("title") or "DashScope 搜索来源"),
            "url": url,
            "source_name": str(source.get("source_name") or "DashScope"),
            "published_date": source.get("published_date") or "",
            "summary_zh": str(source.get("snippet") or "该链接由 DashScope 内置搜索返回，未被模型选为核心消息。"),
            "what_happened_zh": str(source.get("snippet") or "该链接由 DashScope 内置搜索返回。"),
            "why_it_matters_to_ticker_zh": "作为联网搜索透明度附录展示，不单独支撑交易建议。",
            "impact_direction": "neutral",
            "impact_horizon": "short_term",
            "source_verified": True,
            "article_fetch_ok": False,
            "source_quality": "tier_3",
            "verification_level_zh": "DashScope 搜索来源",
            "source_note_only": True,
            "event_type": "搜索来源",
            "content_type": "search_result",
            "supports_action": "watch",
            "does_not_prove_zh": "未被 AI 选为核心消息，不单独支撑方向性交易建议。",
        })
    return items[:12]


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
    analyst_runner=None,
    verbose: bool = True,
) -> dict:
    """Generate Portfolio AI Analyst v3 report.

    The production path performs one model call. Python remains authoritative for
    all prices, weights and metrics. The AI output may enrich interpretation and
    current news, but imperfect citations no longer suppress the full report.
    """
    run_started_at = dt.datetime.now().astimezone().isoformat()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    output = Path(output)

    portfolio_page = payload["portfolio_page"]
    settings = normalize_analyst_settings(portfolio_page.get("analysis_settings") or {})
    settings["model"] = model or settings.get("model") or "deepseek-v4-pro"
    settings["provider"] = provider or "dashscope"
    base_currency = settings.get("base_currency", "EUR")
    benchmark = normalize_yfinance_ticker(settings.get("benchmark") or "^GSPC")
    tickers = list(dict.fromkeys(
        normalize_yfinance_ticker(h.get("ticker"))
        for h in portfolio_page.get("holdings", [])
        if normalize_yfinance_ticker(h.get("ticker"))
    ))

    if close is None:
        close = MarketDataService.fetch_adjusted_close_batch(tickers + [benchmark], period="2y", interval="1d")
    latest_prices = _latest_prices(close)
    market_rows = payload.get("market_rows") or market_rows or _market_rows_from_close(close, portfolio_page)

    currencies = {infer_ticker_currency(t, "") for t in tickers}
    currencies.update(str(h.get("buy_currency") or "").upper() for h in portfolio_page.get("holdings", []))
    if fx_rates is None:
        fx_rates = dict(payload.get("fx_rates") or {}) or _fx_rates(currencies, base_currency)

    enrich = os.environ.get("PORTFOLIO_ENRICH_YFINANCE", "false").strip().lower() in {"1", "true", "yes"}
    instrument_metadata = build_instrument_metadata(portfolio_page, market_rows=market_rows, enrich=enrich)

    snapshot = build_portfolio_snapshot(
        portfolio_page,
        market_rows,
        latest_prices=latest_prices,
        fx_rates=fx_rates,
        base_currency=base_currency,
        benchmark=benchmark,
        instrument_metadata=instrument_metadata,
    )
    snapshot["portfolio_name"] = portfolio_name
    snapshot["analysis_settings"] = settings
    snapshot["report_date"] = dt.date.today().isoformat()
    snapshot["data_cutoffs"] = _data_cutoffs(close, instrument_metadata, benchmark)
    snapshot["as_of_prices"] = snapshot["data_cutoffs"].get("equity") or snapshot["data_cutoffs"].get("etf") or snapshot["report_date"]
    snapshot["run_timeline"] = {
        "run_started_at": run_started_at,
        "snapshot_completed_at": dt.datetime.now().astimezone().isoformat(),
    }

    portfolio_weights = {h["ticker"]: float(h.get("weight") or 0.0) for h in snapshot.get("holdings", [])}
    return_model = build_portfolio_return_model(close, portfolio_weights, benchmark=benchmark)
    metrics = calculate_portfolio_metrics(snapshot, close, benchmark=benchmark, return_model=return_model)
    ranking = rank_portfolio_risks(snapshot, metrics)

    non_finite = scan_non_finite({"metrics": metrics, "summary": snapshot.get("summary", {})})
    snapshot.setdefault("data_quality", {})["non_finite_metrics"] = non_finite
    risk_findings = generate_portfolio_rule_findings(snapshot, metrics, settings, instrument_metadata=instrument_metadata)

    fallback_reason = ""
    analyst_result: dict = {}
    runner = analyst_runner
    try:
        if runner is None:
            analyst_result = run_portfolio_ai_analyst(
                snapshot, metrics, ranking, settings,
                instrument_metadata=instrument_metadata,
                risk_findings=risk_findings,
            )
        else:
            analyst_result = runner(
                snapshot=snapshot,
                metrics=metrics,
                ranking=ranking,
                settings=settings,
                instrument_metadata=instrument_metadata,
                risk_findings=risk_findings,
            )
        if not isinstance(analyst_result, dict) or not isinstance(analyst_result.get("advice"), dict):
            raise PortfolioAnalystOutputError("analyst runner returned no renderable advice")
        advice = analyst_result["advice"]
        diagnostics = dict(analyst_result.get("diagnostics") or {})
        evidence = list(analyst_result.get("news_analysis") or [])
        sources = list(analyst_result.get("sources") or [])
        existing_urls = {str(item.get("url") or "") for item in evidence if item.get("url")}
        source_notes = _search_sources_as_notes(sources, existing_urls)
        report_quality = {
            "publishable": True,
            "actionable": settings.get("advice_mode") != "observe_only",
            "observation_only": settings.get("advice_mode") == "observe_only",
            "architecture": "portfolio_ai_analyst_v3",
        }
    except (PortfolioAnalystUnavailable, PortfolioAnalystOutputError) as exc:
        fallback_reason = f"Portfolio AI Analyst 调用未产生可用结构化结果：{exc}"
        advice = build_quantitative_fallback(snapshot, metrics, ranking, reason=fallback_reason)
        advice = apply_deterministic_action_targets(advice, metrics, settings, return_model=return_model)
        advice["report_style"] = settings.get("report_style")
        advice["report_style_title"] = "量化降级"
        advice["model_name"] = settings.get("model")
        diagnostics = {
            "status": "analyst_fallback",
            "architecture": "portfolio_ai_analyst_v3",
            "search_call_count": 1 if os.getenv("DASHSCOPE_API_KEY") else 0,
            "model_call_count": 1 if os.getenv("DASHSCOPE_API_KEY") else 0,
            "external_search_call_count": 0,
            "retry_count": 0,
            "gap_search_count": 0,
            "model": settings.get("model"),
            "error": str(exc),
        }
        evidence = []
        sources = []
        source_notes = []
        analyst_result = {"raw_model_output": "", "raw_model_payload": {}, "reasoning_content": ""}
        report_quality = {
            "publishable": True,
            "actionable": False,
            "observation_only": True,
            "architecture": "portfolio_ai_analyst_v3",
        }

    if int(diagnostics.get("search_call_count") or 0) > 1 or int(diagnostics.get("model_call_count") or 0) > 1:
        raise RuntimeError("Portfolio AI Analyst v3 违反单调用预算。")
    if int(diagnostics.get("external_search_call_count") or 0) != 0:
        raise RuntimeError("Portfolio AI Analyst v3 禁止外部搜索 API。")

    latest_news_dates = [
        str(item.get("published_date")) for item in evidence + source_notes
        if item.get("published_date")
    ]
    if latest_news_dates:
        snapshot["data_cutoffs"]["news"] = max(latest_news_dates)
    snapshot["run_timeline"]["news_search_completed_at"] = diagnostics.get("generated_at") or dt.datetime.now().astimezone().isoformat()

    # Charts remain deterministic and independent of the model.
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
    if not return_model.cumulative_returns.empty:
        dates = [str(d) for d in return_model.cumulative_returns.index]
        if dates:
            labels = dates
            portfolio_cumulative_pct = (return_model.cumulative_returns * 100).tolist()
            bench = (return_model.benchmark_cumulative_returns * 100).tolist() if not return_model.benchmark_cumulative_returns.empty else []
            if bench and len(bench) == len(dates):
                benchmark_cumulative_pct = bench
    if labels:
        charts["cumulative"] = svg_cumulative_returns(labels, portfolio_cumulative_pct, benchmark_cumulative_pct)

    snapshot["run_timeline"]["report_rendered_at"] = dt.datetime.now().astimezone().isoformat()

    artifacts = {
        "portfolio_snapshot.json": snapshot,
        "portfolio_metrics.json": metrics,
        "portfolio_risk_ranking.json": ranking,
        "portfolio_ai_analyst_advice.json": advice,
        "portfolio_ai_analyst_news.json": evidence,
        "portfolio_ai_analyst_sources.json": sources,
        "portfolio_ai_analyst_diagnostics.json": diagnostics,
        "portfolio_ai_analyst_settings.json": settings,
        "portfolio_report_quality.json": report_quality,
        "portfolio_ai_analyst_source_notes.json": source_notes,
    }
    for name, value in artifacts.items():
        (run_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (run_dir / "portfolio_ai_analyst_raw_output.txt").write_text(
        str(analyst_result.get("raw_model_output") or ""), encoding="utf-8"
    )
    (run_dir / "portfolio_ai_analyst_reasoning.txt").write_text(
        str(analyst_result.get("reasoning_content") or ""), encoding="utf-8"
    )
    (run_dir / "portfolio_ai_analyst_raw_payload.json").write_text(
        json.dumps(analyst_result.get("raw_model_payload") or {}, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    html_text = build_html(
        snapshot, metrics, ranking, advice, evidence,
        instrument_metadata=instrument_metadata,
        settings=settings,
        charts=charts,
        risk_findings=risk_findings,
        cumulative_labels=labels,
        fallback_reason=fallback_reason,
        research_diagnostics=diagnostics,
        report_quality=report_quality,
        source_notes=source_notes,
    )
    output.write_text(html_text, encoding="utf-8")

    if verbose:
        print(f"Portfolio report generated: {output}")
        print(f"Architecture: portfolio_ai_analyst_v3")
        print(f"Model: {settings.get('model')}")
        print(f"Report style: {settings.get('report_style')}")
        print(f"News analysis items: {len(evidence)}")
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
    parser.add_argument("--model", default=os.environ.get("PORTFOLIO_REPORT_MODEL") or "deepseek-v4-pro")
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
