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


def rsi_regime(rsi: float | None) -> str:
    """由 Python 预计算 RSI 区间（修改计划第三轮 18），避免模型自行解释基础区间。

    <30 oversold | 30~40 weak | 40~60 neutral | 60~70 strong | >70 overbought
    """
    if rsi is None:
        return "unknown"
    try:
        v = float(rsi)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(v):
        return "unknown"
    if v < 30:
        return "oversold"
    if v < 40:
        return "weak"
    if v <= 60:
        return "neutral"
    if v <= 70:
        return "strong"
    return "overbought"


def _max_drawdown(series: pd.Series, window: int | None = None) -> float | None:
    s = series.dropna()
    if window:
        s = s.tail(window)
    if len(s) < 2:
        return None
    cumulative = (1.0 + s).cumprod()
    drawdown = cumulative / cumulative.cummax() - 1.0
    return float(drawdown.min() * 100.0)


def calculate_relative_window_return(
    close_window: pd.DataFrame,
    weights: pd.Series,
    benchmark_series: pd.Series,
    periods: int,
    *,
    minimum_weight_coverage: float = 0.90,
) -> dict[str, Any]:
    """计算组合相对基准在 [−periods, 0] 窗口的累计收益（修改计划第三轮 4）。

    关键点：
    - benchmark_series 已 dropna，只取基准有效交易日；
    - 组合与基准使用共同有效起止日期（交集），避免 Saturday 仅 BTC 有值而基准为 NaN；
    - 返回 status=actual 或 insufficient_data，绝不返回 NaN；
    - weight_coverage 低于阈值时如实标注。
    """
    if close_window is None or close_window.empty:
        return {"portfolio": None, "benchmark": None, "relative": None, "status": "insufficient_data"}
    tickers = list(close_window.columns)
    w = weights.reindex(tickers).fillna(0.0)
    if w.sum() <= 0:
        return {"portfolio": None, "benchmark": None, "relative": None, "status": "insufficient_data"}

    valid = close_window.dropna(how="any")
    common = valid.index.intersection(benchmark_series.dropna().index)
    if len(common) < periods + 1:
        return {
            "portfolio": None, "benchmark": None, "relative": None,
            "start_date": None, "end_date": None,
            "weight_coverage": None, "status": "insufficient_data",
        }
    window_idx = common[-(periods + 1):]
    p0 = close_window.loc[window_idx[0]]
    p1 = close_window.loc[window_idx[-1]]
    ret = (p1 / p0 - 1.0).replace([np.inf, -np.inf], np.nan)
    covered = float(w[ret.notna()].sum())
    coverage = covered / float(w.sum())
    if coverage < minimum_weight_coverage:
        return {
            "portfolio": None, "benchmark": None, "relative": None,
            "start_date": str(window_idx[0].date()), "end_date": str(window_idx[-1].date()),
            "weight_coverage": round(coverage, 3), "status": "insufficient_coverage",
        }
    portfolio_return = float((ret * w).sum() * 100.0)
    b0 = float(benchmark_series.loc[window_idx[0]])
    b1 = float(benchmark_series.loc[window_idx[-1]])
    if not (math.isfinite(b0) and math.isfinite(b1) and b0 != 0):
        return {
            "portfolio": portfolio_return, "benchmark": None, "relative": None,
            "start_date": str(window_idx[0].date()), "end_date": str(window_idx[-1].date()),
            "weight_coverage": round(coverage, 3), "status": "benchmark_missing",
        }
    benchmark_return = float((b1 / b0 - 1.0) * 100.0)
    return {
        "portfolio": portfolio_return,
        "benchmark": benchmark_return,
        "relative": portfolio_return - benchmark_return,
        "start_date": str(window_idx[0].date()),
        "end_date": str(window_idx[-1].date()),
        "weight_coverage": round(coverage, 3),
        "status": "actual",
    }


# 不同工具类型的单日异常阈值（修改计划第三轮 5）。
_ANOMALY_THRESHOLD = {
    "EQUITY": 0.50, "ETF": 0.50, "FUND": 0.50, "INDEX": 0.50,
    "ETC": 0.50, "COMMODITY": 0.50, "CRYPTO": 1.00, "UNKNOWN": 0.50,
}


def validate_return_series(
    ticker: str,
    series: pd.Series,
    instrument_type: str = "UNKNOWN",
) -> list[dict[str, Any]]:
    """对单个 ticker 的日收益做质量控制，标记可能的拆股/货币/数据源异常。

    普通股票/ETF 单日绝对收益 > 50% 即标记；加密资产使用更高阈值。
    """
    anomalies: list[dict[str, Any]] = []
    threshold = _ANOMALY_THRESHOLD.get(str(instrument_type).upper(), 0.50)
    s = series.dropna()
    for date, ret in s.items():
        try:
            r = float(ret)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(r):
            continue
        if abs(r) > threshold:
            anomalies.append({
                "ticker": ticker,
                "date": str(pd.Timestamp(date).date()),
                "return_pct": round(r * 100.0, 2),
                "instrument_type": instrument_type,
                "reason": "possible split, currency unit or provider anomaly",
            })
    return anomalies


def _detect_return_anomalies(
    close: pd.DataFrame,
    tickers: list[str],
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """扫描所有持仓日收益，返回异常清单与异常权重占比。"""
    instrument_metadata = instrument_metadata or {}
    anomalies: list[dict[str, Any]] = []
    anomaly_weight = 0.0
    if close is None or close.empty:
        return {"anomalies": anomalies, "anomaly_weight": 0.0, "anomaly_tickers": []}
    cols = [c for c in tickers if c in close.columns]
    for ticker in cols:
        itype = str(instrument_metadata.get(ticker, {}).get("instrument_type") or "UNKNOWN").upper()
        series = close[ticker].dropna()
        rets = series.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
        found = validate_return_series(ticker, rets, itype)
        if found:
            anomalies.extend(found)
            anomaly_weight += float(instrument_metadata.get(ticker, {}).get("weight") or 0.0)
    return {
        "anomalies": anomalies,
        "anomaly_weight": round(anomaly_weight, 4),
        "anomaly_tickers": sorted({a["ticker"] for a in anomalies}),
    }


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

    for window_name, field in (("1D", "return_1d"), ("YTD", "return_ytd")):
        vals = []
        wts = []
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

    # 相对收益（修改计划第三轮 4）：使用基准有效日期与共同有效起止日，
    # 不再用 `if b0:` 这类对 NaN 为真值的判定。
    bench_series = close[benchmark].dropna() if benchmark in close.columns else pd.Series(dtype=float)
    if not bench_series.empty:
        for name, periods in RETURN_WINDOWS.items():
            if len(close) > periods + 1:
                metrics["relative_returns"][name] = calculate_relative_window_return(
                    close[ticker_cols], aligned_weights, bench_series, periods,
                    minimum_weight_coverage=0.90,
                )

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
                # 修改计划 13.3：采用方案 B（仅展示正风险预算并重新归一化），
                # 避免直接 max(0, x) 后不归一化导致风险贡献之和漂移。
                raw_rc = {ticker: float(value) for ticker, value in zip(cov.index, component / total_component)}
                positive = {t: max(0.0, v) for t, v in raw_rc.items()}
                pos_sum = sum(positive.values())
                if pos_sum > 0:
                    positive = {t: v / pos_sum for t, v in positive.items()}
                metrics["risk_contributions"] = [
                    {
                        "ticker": ticker,
                        "weight": float(weights_by_ticker.get(ticker, 0.0)),
                        "risk_contribution": positive.get(ticker, 0.0),
                        "risk_weight_gap": positive.get(ticker, 0.0) - float(weights_by_ticker.get(ticker, 0.0)),
                    }
                    for ticker in (raw_rc.keys() if False else cov.index)
                ]

    # 修改计划 13.1 / 13.2：基于历史日收益计算每个持仓的真实波动率与回撤，
    # 不再用 Beta 或 1M 收益近似。
    metrics["holdings_detail"] = _holdings_detail(close, tickers)

    # 修改计划第三轮 5 / 7：收益异常检测 + 累计收益方法学披露。
    metrics["return_anomalies"] = _detect_return_anomalies(
        close, tickers, instrument_metadata=snapshot.get("instrument_metadata") or {}
    )
    metrics["performance_methodology"] = {
        "method": "static_current_weight_backtest",
        "is_actual_portfolio_history": False,
        # 历史收益尚未统一转换为基础货币（修改计划第三轮 8， deferred）：
        # 当前相对收益为「本地货币近似」，不得作为精确 Alpha 或高置信度结论。
        "base_currency_adjusted": False,
        "historical_fx_aligned": False,
        "lookback_days": 252,
        "benchmark": benchmark,
    }

    # 修改计划第三轮 20：Python 预计算聚合值，禁止模型自行求和/平均/推导数值。
    metrics["aggregates"] = _compute_aggregates(holdings, metrics)
    # 修改计划第三轮 22：Python 风险评分，AI 只负责解释（最多凭新鲜证据上调一级）。
    risk_score = compute_portfolio_risk_score(metrics)
    metrics["portfolio_risk_score"] = risk_score["score"]
    metrics["portfolio_risk_level"] = risk_score["level"]
    metrics["risk_score_components"] = risk_score["components"]

    _technical_breadth_from_snapshot(metrics, holdings)
    return metrics


def _compute_aggregates(holdings: list[dict[str, Any]], metrics: dict[str, Any]) -> dict[str, float | None]:
    """修改计划第三轮 20：所有聚合数字由 Python 预计算，模型只能引用，不得自行算术。"""
    top_risk = sorted(
        metrics.get("risk_contributions", []) or [],
        key=lambda x: -(float(x.get("risk_contribution") or 0.0)),
    )[:5]
    top_risk_contribution_sum = float(sum(float(x.get("risk_contribution") or 0.0) for x in top_risk))
    top_risk_weight_sum = float(sum(float(x.get("weight") or 0.0) for x in top_risk))
    high_beta_weight = float(sum(
        float(h.get("weight") or 0.0)
        for h in holdings
        if (_safe_float(h.get("beta")) or 0.0) > 1.5
    ))
    below_ema50_weight = float(sum(
        float(h.get("weight") or 0.0)
        for h in holdings
        if _safe_float(h.get("price_vs_ema50_pct")) is not None and _safe_float(h.get("price_vs_ema50_pct")) < 0
    ))
    return {
        "top_risk_contribution_sum": round(top_risk_contribution_sum, 4),
        "top_risk_weight_sum": round(top_risk_weight_sum, 4),
        "high_beta_weight": round(high_beta_weight, 4),
        "below_ema50_weight": round(below_ema50_weight, 4),
        # 计划减仓释放的权重需结合 AI 操作建议，延后在主流程计算并回填。
        "recommended_reduction_weight": None,
    }


def _score_band(value: float | None, bands: list[tuple[float, int]]) -> int:
    """bands: 升序 (阈值, 分值)；value <= 阈值取对应分值，超过全部阈值取最大值。"""
    if value is None or not math.isfinite(value):
        return 0
    last = 0
    for threshold, score in bands:
        last = score
        if value <= threshold:
            return score
    return last


def compute_portfolio_risk_score(metrics: dict[str, Any]) -> dict[str, Any]:
    """修改计划第三轮 22：Python 风险评分，确定性支撑风险等级（AI 仅可解释/有限上调）。

    子项各 0~15，合计 0~100；映射：0~30 低 / 30~50 中等 / 50~70 中高 / 70~100 高。
    """
    top1 = _safe_float(metrics.get("top1_weight"))
    beta = _safe_float(metrics.get("portfolio_beta"))
    vol = _safe_float(metrics.get("annualized_volatility"))
    dd = _safe_float(metrics.get("max_drawdown_252d"))
    corr = _safe_float(metrics.get("max_pairwise_correlation"))
    breadth = _safe_float((metrics.get("technical_breadth", {}) or {}).get("below_ema50_weight"))
    top_risk_sum = _safe_float((metrics.get("aggregates", {}) or {}).get("top_risk_contribution_sum"))

    components = {
        "concentration": _score_band(top1, [(0.05, 0), (0.10, 4), (0.20, 9), (0.30, 13), (1e9, 15)]),
        "beta": _score_band(beta, [(0.8, 0), (1.0, 3), (1.25, 7), (1.5, 11), (1e9, 15)]),
        "volatility": _score_band(vol, [(10, 0), (15, 5), (20, 9), (30, 13), (1e9, 15)]),
        "drawdown": _score_band(dd, [(-10, 0), (-20, 5), (-30, 9), (-45, 13), (-1e9, 15)]),
        "correlation": _score_band(corr, [(0.5, 0), (0.7, 4), (0.85, 9), (0.95, 13), (1e9, 15)]),
        "breadth": _score_band(breadth, [(0.3, 0), (0.5, 4), (0.7, 9), (0.85, 13), (1e9, 15)]),
        "risk_contribution": _score_band(top_risk_sum, [(0.3, 0), (0.5, 4), (0.65, 9), (0.8, 13), (1e9, 15)]),
    }
    score = int(round(sum(components.values())))
    if score < 30:
        level = "low"
    elif score < 50:
        level = "medium"
    elif score < 70:
        level = "medium_high"
    else:
        level = "high"
    return {"score": score, "level": level, "components": components}


def _holdings_detail(close: pd.DataFrame, tickers: list[str]) -> dict[str, dict[str, float | None]]:
    detail: dict[str, dict[str, float | None]] = {}
    if close is None or close.empty:
        return detail
    cols = [c for c in tickers if c in close.columns]
    if not cols:
        return detail
    prices = close[cols]
    for ticker in cols:
        series = prices[ticker].dropna()
        rec: dict[str, float | None] = {
            "annualized_volatility": None,
            "max_drawdown_63d": None,
            "max_drawdown_252d": None,
            "distance_from_52w_high": None,
        }
        if len(series) >= 5:
            rets = series.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
            if len(rets) >= 2:
                rec["annualized_volatility"] = float(rets.std() * math.sqrt(252) * 100.0)
                rec["max_drawdown_63d"] = _max_drawdown(rets, 63)
                rec["max_drawdown_252d"] = _max_drawdown(rets, 252)
        if len(series) >= 20:
            high = float(series.tail(252).max())
            last = float(series.iloc[-1])
            if high > 0:
                rec["distance_from_52w_high"] = float((last / high - 1.0) * 100.0)
        detail[ticker] = rec
    return detail


def _technical_breadth_from_snapshot(metrics: dict[str, Any], holdings: list[dict[str, Any]]) -> None:
    total_weight = sum(float(h.get("weight") or 0.0) for h in holdings)
    def weight_sum(predicate):
        if total_weight <= 0:
            return None
        return sum(float(h.get("weight") or 0.0) for h in holdings if predicate(h)) / total_weight
    metrics["technical_breadth"] = {
        "above_ema20_weight": weight_sum(lambda h: _safe_float(h.get("price_vs_ema20_pct")) is not None and _safe_float(h.get("price_vs_ema20_pct")) >= 0),
        "above_ema50_weight": weight_sum(lambda h: _safe_float(h.get("price_vs_ema50_pct")) is not None and _safe_float(h.get("price_vs_ema50_pct")) >= 0),
        "above_ema200_weight": weight_sum(lambda h: _safe_float(h.get("price_vs_ema200_pct")) is not None and _safe_float(h.get("price_vs_ema200_pct")) >= 0),
        "rsi_over_70_weight": weight_sum(lambda h: _safe_float(h.get("rsi")) is not None and _safe_float(h.get("rsi")) > 70),
        "rsi_under_30_weight": weight_sum(lambda h: _safe_float(h.get("rsi")) is not None and _safe_float(h.get("rsi")) < 30),
    }
