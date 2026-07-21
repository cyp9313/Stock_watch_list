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
    """Tie-aware percentile（修改计划第三轮 31）。

    相同数值获得相同百分位（平均排名 / n），不再按顺序给不同排名。
    等价于 pandas Series.rank(method="average", pct=True)。
    """
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1])
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 0.5}
    ranks: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j < n and ordered[j][1] == ordered[i][1]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[ordered[k][0]] = avg_rank / n
        i = j
    return ranks


def rank_portfolio_risks(snapshot: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    holdings = snapshot.get("holdings", []) or []
    risk_contrib = {
        item["ticker"]: _value(item.get("risk_contribution"))
        for item in metrics.get("risk_contributions", []) or []
    }
    weights = {h["ticker"]: _value(h.get("weight")) for h in holdings}
    detail = metrics.get("holdings_detail", {}) or {}

    # 修改计划 13.1 / 13.2：优先使用基于历史日收益的真实波动率与回撤；
    # 数据缺失时降级为 |Beta| 或 |1M 收益| 近似，保证排名始终可计算。
    volatility = {}
    drawdown = {}
    for holding in holdings:
        ticker = holding["ticker"]
        d = detail.get(ticker, {}) or {}
        vol = d.get("annualized_volatility")
        dd = d.get("max_drawdown_63d")
        volatility[ticker] = _value(vol) if vol is not None else abs(_value(holding.get("beta")))
        drawdown[ticker] = abs(_value(dd)) if dd is not None else abs(min(0.0, _value(holding.get("return_1m"))))

    technical = {}
    for holding in holdings:
        ticker = holding["ticker"]
        score = 0.0
        if _value(holding.get("price_vs_ema20_pct")) < 0:
            score += 0.25
        if _value(holding.get("price_vs_ema50_pct")) < 0:
            score += 0.25
        if _value(holding.get("price_vs_ema200_pct")) < 0:
            score += 0.15
        rsi = _value(holding.get("rsi"), 50.0)
        if rsi > 70 or rsi < 30:
            score += 0.15
        if _value(holding.get("return_5d")) < -5:
            score += 0.20
        # 距离 52 周高点越远（越负）技术面越弱
        dist = _value((detail.get(ticker, {}) or {}).get("distance_from_52w_high"))
        if dist < -10:
            score += 0.10
        technical[ticker] = min(1.0, score)

    rc_pct = _percentiles(risk_contrib or weights)
    weight_pct = _percentiles(weights)
    vol_pct = _percentiles(volatility)
    drawdown_pct = _percentiles(drawdown)
    technical_pct = _percentiles(technical)

    # 修改计划 13.5：风险优先级评分权重。news_risk_pre_score 在搜索前为 0。
    news_risk_pre = {h["ticker"]: 0.0 for h in holdings}
    news_risk_pct = _percentiles(news_risk_pre)

    ranked = []
    for holding in holdings:
        ticker = holding["ticker"]
        score = (
            0.30 * rc_pct.get(ticker, 0.0)
            + 0.20 * weight_pct.get(ticker, 0.0)
            + 0.15 * vol_pct.get(ticker, 0.0)
            + 0.15 * drawdown_pct.get(ticker, 0.0)
            + 0.15 * technical_pct.get(ticker, 0.0)
            + 0.05 * news_risk_pct.get(ticker, 0.0)
        )
        ranked.append({
            "ticker": ticker,
            "group": holding.get("group"),
            "weight": weights.get(ticker, 0.0),
            "risk_contribution": risk_contrib.get(ticker),
            "annualized_volatility": volatility.get(ticker),
            "max_drawdown_63d": drawdown.get(ticker),
            "technical_risk_score": technical.get(ticker, 0.0),
            "risk_priority_score": score,
        })
    ranked.sort(key=lambda item: item["risk_priority_score"], reverse=True)
    risk_priority_rank = {item["ticker"]: i for i, item in enumerate(ranked, start=1)}
    contribution_order = sorted(ranked, key=lambda item: -_value(item.get("risk_contribution")))
    contribution_rank = {item["ticker"]: i for i, item in enumerate(contribution_order, start=1)}
    weight_order = sorted(ranked, key=lambda item: -_value(item.get("weight")))
    weight_rank = {item["ticker"]: i for i, item in enumerate(weight_order, start=1)}
    volatility_order = sorted(ranked, key=lambda item: -_value(item.get("annualized_volatility")))
    volatility_rank = {item["ticker"]: i for i, item in enumerate(volatility_order, start=1)}
    for item in ranked:
        ticker = item["ticker"]
        item["risk_priority_rank"] = risk_priority_rank[ticker]
        item["risk_contribution_rank"] = contribution_rank[ticker]
        item["weight_rank"] = weight_rank[ticker]
        item["volatility_rank"] = volatility_rank[ticker]

    max_tickers = max(1, min(10, int(os.environ.get("PORTFOLIO_SINGLE_SEARCH_TOP_TICKERS", "5") or "5")))
    configured = snapshot.get("analysis_settings", {}).get("research_max_tickers") if isinstance(snapshot.get("analysis_settings"), dict) else None
    if configured:
        max_tickers = min(max_tickers, max(1, int(configured)))
    count = min(len(ranked), max_tickers)
    return {
        "items": ranked,
        "top_risk_tickers": [item["ticker"] for item in ranked[:count]],
        "research_ticker_count": count,
    }
