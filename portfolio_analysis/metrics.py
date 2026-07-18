from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


RETURN_WINDOWS = {"5D": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _max_drawdown(series: pd.Series, window: int | None = None) -> float | None:
    s = series.dropna()
    if window:
        s = s.tail(window)
    if len(s) < 2:
        return None
    cumulative = (1.0 + s).cumprod()
    drawdown = cumulative / cumulative.cummax() - 1.0
    return float(drawdown.min() * 100.0)


def calculate_portfolio_metrics(
    snapshot: dict[str, Any],
    close_prices: pd.DataFrame | None = None,
    *,
    benchmark: str | None = None,
) -> dict[str, Any]:
    holdings = [h for h in snapshot.get("holdings", []) if _safe_float(h.get("weight")) and _safe_float(h.get("weight")) > 0]
    weights = np.array([float(h["weight"]) for h in holdings], dtype=float)
    tickers = [h["ticker"] for h in holdings]
    top_weights = sorted(weights.tolist(), reverse=True)
    hhi = float(np.sum(weights ** 2)) if len(weights) else None
    metrics: dict[str, Any] = {
        "top1_weight": top_weights[0] if top_weights else None,
        "top3_weight": float(sum(top_weights[:3])) if top_weights else None,
        "hhi": hhi,
        "hhi_10000": hhi * 10000.0 if hhi is not None else None,
        "effective_holdings": 1.0 / hhi if hhi else None,
        "portfolio_beta": None,
        "annualized_volatility": None,
        "max_drawdown_63d": None,
        "max_drawdown_252d": None,
        "average_pairwise_correlation": None,
        "max_pairwise_correlation": None,
        "high_correlation_pairs": [],
        "risk_contributions": [],
        "relative_returns": {},
        "technical_breadth": {},
    }

    for window_name in ("1D", "5D", "1M", "YTD"):
        vals = []
        wts = []
        field = {"1D": "return_1d", "5D": "return_5d", "1M": "return_1m", "YTD": "return_ytd"}[window_name]
        for holding in holdings:
            ret = _safe_float(holding.get(field))
            weight = _safe_float(holding.get("weight"))
            if ret is not None and weight is not None:
                vals.append(ret)
                wts.append(weight)
        metrics["relative_returns"][window_name] = float(np.average(vals, weights=wts)) if vals and sum(wts) else None

    if close_prices is None or close_prices.empty or not holdings:
        _technical_breadth_from_snapshot(metrics, holdings)
        return metrics

    close = close_prices.copy()
    close.columns = [str(c).upper() for c in close.columns]
    benchmark = (benchmark or snapshot.get("benchmark") or "^GSPC").upper()
    ticker_cols = [ticker for ticker in tickers if ticker in close.columns]
    if not ticker_cols:
        _technical_breadth_from_snapshot(metrics, holdings)
        return metrics

    returns = close[ticker_cols].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
    weights_by_ticker = pd.Series({h["ticker"]: float(h["weight"]) for h in holdings})
    aligned_weights = weights_by_ticker.reindex(ticker_cols).fillna(0.0)
    if aligned_weights.sum() > 0:
        aligned_weights = aligned_weights / aligned_weights.sum()
    portfolio_returns = (returns * aligned_weights).sum(axis=1, min_count=1).dropna()

    if len(portfolio_returns) >= 20:
        metrics["annualized_volatility"] = float(portfolio_returns.std() * math.sqrt(252) * 100.0)
        metrics["max_drawdown_63d"] = _max_drawdown(portfolio_returns, 63)
        metrics["max_drawdown_252d"] = _max_drawdown(portfolio_returns, 252)

    if benchmark in close.columns and len(portfolio_returns) >= 20:
        bench_returns = close[benchmark].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
        joined = pd.concat([portfolio_returns.rename("portfolio"), bench_returns.rename("benchmark")], axis=1).dropna()
        if len(joined) >= 20 and joined["benchmark"].var() != 0:
            metrics["portfolio_beta"] = float(joined["portfolio"].cov(joined["benchmark"]) / joined["benchmark"].var())
        for name, periods in RETURN_WINDOWS.items():
            if len(close) > periods:
                p0 = (close[ticker_cols].iloc[-periods - 1]).replace(0, np.nan)
                p1 = close[ticker_cols].iloc[-1]
                holding_return = (p1 / p0 - 1.0).replace([np.inf, -np.inf], np.nan)
                portfolio_return = float((holding_return * aligned_weights).sum() * 100.0)
                b0 = close[benchmark].iloc[-periods - 1]
                b1 = close[benchmark].iloc[-1]
                benchmark_return = float((b1 / b0 - 1.0) * 100.0) if b0 else None
                metrics["relative_returns"][name] = {
                    "portfolio": portfolio_return,
                    "benchmark": benchmark_return,
                    "relative": portfolio_return - benchmark_return if benchmark_return is not None else None,
                }

    corr = returns[ticker_cols].dropna(how="all").corr()
    if corr.shape[0] >= 2:
        mask = np.triu(np.ones(corr.shape), k=1).astype(bool)
        values = corr.where(mask).stack().dropna()
        if not values.empty:
            metrics["average_pairwise_correlation"] = float(values.mean())
            metrics["max_pairwise_correlation"] = float(values.max())
            metrics["high_correlation_pairs"] = [
                {"ticker_a": a, "ticker_b": b, "correlation": float(v)}
                for (a, b), v in values.sort_values(ascending=False).head(5).items()
                if v >= 0.75
            ]

    cov = returns[ticker_cols].dropna(how="all").cov()
    if cov.shape[0] and aligned_weights.sum() > 0:
        w = aligned_weights.reindex(cov.index).fillna(0.0).to_numpy()
        cov_matrix = cov.to_numpy()
        variance = float(w.T @ cov_matrix @ w)
        if variance > 0:
            volatility = math.sqrt(variance)
            marginal = cov_matrix @ w / volatility
            component = w * marginal
            total_component = float(component.sum())
            if total_component != 0:
                rc = component / total_component
                metrics["risk_contributions"] = [
                    {
                        "ticker": ticker,
                        "weight": float(weights_by_ticker.get(ticker, 0.0)),
                        "risk_contribution": float(max(0.0, value)),
                        "risk_weight_gap": float(max(0.0, value) - weights_by_ticker.get(ticker, 0.0)),
                    }
                    for ticker, value in zip(cov.index, rc)
                ]

    _technical_breadth_from_snapshot(metrics, holdings)
    return metrics


def _technical_breadth_from_snapshot(metrics: dict[str, Any], holdings: list[dict[str, Any]]) -> None:
    total_weight = sum(float(h.get("weight") or 0.0) for h in holdings)
    def weight_sum(predicate):
        if total_weight <= 0:
            return None
        return sum(float(h.get("weight") or 0.0) for h in holdings if predicate(h)) / total_weight
    metrics["technical_breadth"] = {
        "above_ema20_weight": weight_sum(lambda h: _safe_float(h.get("diff_ema20")) is not None and _safe_float(h.get("diff_ema20")) >= 0),
        "above_ema50_weight": weight_sum(lambda h: _safe_float(h.get("diff_ema50")) is not None and _safe_float(h.get("diff_ema50")) >= 0),
        "above_ema200_weight": weight_sum(lambda h: _safe_float(h.get("diff_ema200")) is not None and _safe_float(h.get("diff_ema200")) >= 0),
        "rsi_over_70_weight": weight_sum(lambda h: _safe_float(h.get("rsi")) is not None and _safe_float(h.get("rsi")) > 70),
        "rsi_under_30_weight": weight_sum(lambda h: _safe_float(h.get("rsi")) is not None and _safe_float(h.get("rsi")) < 30),
    }
