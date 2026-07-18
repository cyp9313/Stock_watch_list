from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys
from pathlib import Path

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
from portfolio_analysis.rules import generate_portfolio_rule_findings
from portfolio_analysis.snapshot import infer_ticker_currency
from daily_report.src.stock_daily_agent.research_service import ResearchService
from ticker_mapping import normalize_yfinance_ticker


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


def _build_rule_advice(snapshot: dict, metrics: dict, ranking: dict, evidence: list[dict], settings: dict) -> dict:
    findings = generate_portfolio_rule_findings(snapshot, metrics, settings)
    rc = {item.get("ticker"): item for item in metrics.get("risk_contributions", [])}
    actions = []
    max_position = float(settings.get("max_position_pct", 20.0) or 20.0) / 100.0
    for priority, item in enumerate(ranking.get("items", [])[: max(1, min(8, len(ranking.get("items", []))))], start=1):
        ticker = item["ticker"]
        weight = float(item.get("weight") or 0.0)
        risk_contribution = (rc.get(ticker) or {}).get("risk_contribution")
        action = "hold"
        if weight > max_position * 1.15 or (risk_contribution is not None and risk_contribution > weight * 1.5):
            action = "trim" if settings.get("allow_reduce", True) else "watch"
        elif weight < max_position * 0.45 and settings.get("allow_add", True):
            action = "watch"
        actions.append({
            "ticker": ticker,
            "action": action,
            "priority": priority,
            "current_weight": weight,
            "target_weight_min": max(0.0, min(weight, max_position * 0.85)),
            "target_weight_max": min(max_position, max(weight, max_position)),
            "confidence": 0.55 if not evidence else 0.68,
            "portfolio_reason": "Risk priority score is high relative to the rest of the portfolio.",
            "technical_reason": "Technical indicators and recent returns were scored by deterministic Python rules.",
            "news_reason": "See evidence section; if evidence is empty, search providers were unavailable or returned no results.",
            "trigger_conditions": [
                "Review if the position's risk contribution rises further or price weakens below key moving averages."
            ],
            "invalidation_conditions": [
                "Reassess after material earnings upgrades, lower volatility, or improved diversification."
            ],
            "evidence_ids": [e["evidence_id"] for e in evidence if e.get("ticker") == ticker][:3],
        })
    stance = "constructive_watch" if metrics.get("top3_weight", 0) and metrics.get("top3_weight", 0) < 0.6 else "risk_control_first"
    return {
        "portfolio_stance": stance,
        "risk_level": "medium_high" if findings else "medium",
        "confidence": 0.62 if evidence else 0.48,
        "summary": "This portfolio report combines deterministic portfolio risk metrics with top-risk evidence where search providers are configured.",
        "key_risks": findings,
        "actions": actions,
        "watch_items": [{"title": "Top-risk holdings", "reason": ", ".join(ranking.get("top_risk_tickers") or [])}],
        "data_limitations": snapshot.get("data_quality", {}),
        "disclaimer": "This report is for research purposes only and is not investment advice.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portfolio-input", required=True)
    parser.add_argument("--portfolio-id", required=True)
    parser.add_argument("--portfolio-name", required=True)
    parser.add_argument("--owner-scope", required=True)
    parser.add_argument("--search-provider", default="auto")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(Path(args.portfolio_input).read_text(encoding="utf-8"))
    portfolio_page = payload["portfolio_page"]
    settings = dict(portfolio_page.get("analysis_settings") or {})
    base_currency = settings.get("base_currency", "EUR")
    benchmark = normalize_yfinance_ticker(settings.get("benchmark") or "^GSPC")
    tickers = list(dict.fromkeys(
        normalize_yfinance_ticker(h.get("ticker"))
        for h in portfolio_page.get("holdings", [])
        if normalize_yfinance_ticker(h.get("ticker"))
    ))
    close = MarketDataService.fetch_adjusted_close_batch(tickers + [benchmark], period="1y", interval="1d")
    latest_prices = _latest_prices(close)
    market_rows = payload.get("market_rows") or _market_rows_from_close(close, portfolio_page)
    currencies = {infer_ticker_currency(t, "") for t in tickers}
    currencies.update(str(h.get("buy_currency") or "").upper() for h in portfolio_page.get("holdings", []))
    fx_rates = dict(payload.get("fx_rates") or {})
    if not fx_rates:
        fx_rates = _fx_rates(currencies, base_currency)

    snapshot = build_portfolio_snapshot(
        portfolio_page,
        market_rows,
        latest_prices=latest_prices,
        fx_rates=fx_rates,
        base_currency=base_currency,
        benchmark=benchmark,
    )
    snapshot["analysis_settings"] = settings
    snapshot["report_date"] = dt.date.today().isoformat()
    metrics = calculate_portfolio_metrics(snapshot, close, benchmark=benchmark)
    ranking = rank_portfolio_risks(snapshot, metrics)

    queries = []
    for ticker in ranking.get("top_risk_tickers", []):
        queries.append(f"{ticker} latest earnings guidance analyst rating risk stock news")
    top_groups = {}
    for holding in snapshot.get("holdings", []):
        top_groups[holding["group"]] = top_groups.get(holding["group"], 0.0) + float(holding.get("weight") or 0.0)
    for group, _ in sorted(top_groups.items(), key=lambda item: item[1], reverse=True)[:2]:
        queries.append(f"{group} sector latest market risks stocks")
    queries.append(f"{benchmark} interest rates macro market risk outlook")
    try:
        evidence = ResearchService().search(queries, provider=args.search_provider, max_results=3)
    except Exception as exc:
        evidence = [{"evidence_id": "E000", "scope": "research", "title": "Research unavailable", "source": "system", "summary": str(exc), "url": ""}]
    ticker_set = set(ranking.get("top_risk_tickers", []))
    for item in evidence:
        for ticker in ticker_set:
            if ticker in (item.get("query") or "") or ticker in (item.get("title") or ""):
                item["ticker"] = ticker
                break
        item.setdefault("scope", "portfolio")

    advice = validate_portfolio_advice(_build_rule_advice(snapshot, metrics, ranking, evidence, settings), snapshot, evidence)

    paths = {
        "snapshot": run_dir / "portfolio_snapshot.json",
        "metrics": run_dir / "portfolio_metrics.json",
        "ranking": run_dir / "portfolio_risk_ranking.json",
        "evidence": run_dir / "portfolio_evidence.json",
        "advice": run_dir / "portfolio_advice.json",
    }
    for key, value in (("snapshot", snapshot), ("metrics", metrics), ("ranking", ranking), ("evidence", evidence), ("advice", advice)):
        paths[key].write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    builder = Path(__file__).resolve().parent / "scripts" / "build_portfolio_report.py"
    subprocess.run(
        [
            sys.executable,
            str(builder),
            "--snapshot", str(paths["snapshot"]),
            "--metrics", str(paths["metrics"]),
            "--risk-ranking", str(paths["ranking"]),
            "--advice", str(paths["advice"]),
            "--evidence", str(paths["evidence"]),
            "--output", str(Path(args.output)),
        ],
        check=True,
    )
    print(f"Portfolio report generated: {args.output}")
    print(f"Top-risk tickers: {', '.join(ranking.get('top_risk_tickers') or [])}")
    print(f"Evidence count: {len(evidence)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
