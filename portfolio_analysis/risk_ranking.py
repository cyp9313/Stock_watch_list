from __future__ import annotations

import math
import os
from typing import Any


def _value(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1])
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    return {ticker: rank / (len(ordered) - 1) for rank, (ticker, _) in enumerate(ordered)}


def rank_portfolio_risks(snapshot: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    holdings = snapshot.get("holdings", []) or []
    risk_contrib = {
        item["ticker"]: _value(item.get("risk_contribution"))
        for item in metrics.get("risk_contributions", []) or []
    }
    weights = {h["ticker"]: _value(h.get("weight")) for h in holdings}
    volatility = {h["ticker"]: abs(_value(h.get("beta"))) for h in holdings}
    drawdown = {h["ticker"]: abs(min(0.0, _value(h.get("return_1m")))) for h in holdings}
    technical = {}
    for holding in holdings:
        score = 0.0
        if _value(holding.get("diff_ema20")) < 0:
            score += 0.25
        if _value(holding.get("diff_ema50")) < 0:
            score += 0.25
        if _value(holding.get("diff_ema200")) < 0:
            score += 0.15
        rsi = _value(holding.get("rsi"), 50.0)
        if rsi > 70 or rsi < 30:
            score += 0.15
        if _value(holding.get("return_5d")) < -5:
            score += 0.20
        technical[holding["ticker"]] = min(1.0, score)

    rc_pct = _percentiles(risk_contrib or weights)
    weight_pct = _percentiles(weights)
    vol_pct = _percentiles(volatility)
    drawdown_pct = _percentiles(drawdown)
    technical_pct = _percentiles(technical)

    ranked = []
    for holding in holdings:
        ticker = holding["ticker"]
        score = (
            0.35 * rc_pct.get(ticker, 0.0)
            + 0.25 * weight_pct.get(ticker, 0.0)
            + 0.20 * technical_pct.get(ticker, 0.0)
            + 0.10 * drawdown_pct.get(ticker, 0.0)
            + 0.10 * vol_pct.get(ticker, 0.0)
        )
        ranked.append({
            "ticker": ticker,
            "group": holding.get("group"),
            "weight": weights.get(ticker, 0.0),
            "risk_contribution": risk_contrib.get(ticker),
            "technical_risk_score": technical.get(ticker, 0.0),
            "risk_priority_score": score,
        })
    ranked.sort(key=lambda item: item["risk_priority_score"], reverse=True)

    max_tickers = int(os.environ.get("PORTFOLIO_RESEARCH_MAX_TICKERS", "5") or "5")
    min_tickers = int(os.environ.get("PORTFOLIO_RESEARCH_MIN_TICKERS", "3") or "3")
    configured = snapshot.get("analysis_settings", {}).get("research_max_tickers") if isinstance(snapshot.get("analysis_settings"), dict) else None
    if configured:
        max_tickers = min(max_tickers, int(configured))
    count = min(len(ranked), max_tickers, max(1, min_tickers, math.ceil(len(ranked) * 0.25)))
    return {
        "items": ranked,
        "top_risk_tickers": [item["ticker"] for item in ranked[:count]],
        "research_ticker_count": count,
    }
