from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


RETURN_WINDOWS = {"5D": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}
RISK_COMPONENT_WEIGHTS = {
    "concentration": 15,
    "beta": 15,
    "volatility": 15,
    "drawdown": 15,
    "correlation": 10,
    "breadth": 15,
    "risk_contribution": 15,
}


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

    benchmark_valid = benchmark_series.dropna().sort_index()
    if len(benchmark_valid) < periods + 1:
        return {
            "portfolio": None, "benchmark": None, "relative": None,
            "start_date": None, "end_date": None,
            "weight_coverage": None, "status": "insufficient_data",
        }
    end_date = benchmark_valid.index[-1]
    start_date = benchmark_valid.index[-(periods + 1)]
    ticker_returns: dict[str, float] = {}
    for ticker in tickers:
        series = close_window[ticker].dropna().sort_index()
        before_start = series.loc[series.index <= start_date]
        before_end = series.loc[series.index <= end_date]
        if before_start.empty or before_end.empty:
            continue
        p0 = _safe_float(before_start.iloc[-1])
        p1 = _safe_float(before_end.iloc[-1])
        if p0 is None or p1 is None or p0 <= 0:
            continue
        ticker_returns[ticker] = p1 / p0 - 1.0
    ret = pd.Series(ticker_returns, dtype=float).reindex(tickers)
    covered = float(w[ret.notna()].sum())
    coverage = covered / float(w.sum())
    if coverage < minimum_weight_coverage:
        return {
            "portfolio": None, "benchmark": None, "relative": None,
            "start_date": str(pd.Timestamp(start_date).date()), "end_date": str(pd.Timestamp(end_date).date()),
            "weight_coverage": round(coverage, 3), "status": "insufficient_coverage",
        }
    portfolio_return = float((ret * w).sum() / covered * 100.0)
    b0 = float(benchmark_valid.loc[start_date])
    b1 = float(benchmark_valid.loc[end_date])
    if not (math.isfinite(b0) and math.isfinite(b1) and b0 != 0):
        return {
            "portfolio": portfolio_return, "benchmark": None, "relative": None,
            "start_date": str(pd.Timestamp(start_date).date()), "end_date": str(pd.Timestamp(end_date).date()),
            "weight_coverage": round(coverage, 3), "status": "benchmark_missing",
        }
    benchmark_return = float((b1 / b0 - 1.0) * 100.0)
    return {
        "portfolio": portfolio_return,
        "benchmark": benchmark_return,
        "relative": portfolio_return - benchmark_return,
        "start_date": str(pd.Timestamp(start_date).date()),
        "end_date": str(pd.Timestamp(end_date).date()),
        "weight_coverage": round(coverage, 3),
        "status": "actual",
    }


def calculate_portfolio_beta(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    min_observations: int = 60,
) -> dict[str, Any]:
    """Calculate historical portfolio beta on common finite benchmark trading days."""
    aligned = pd.concat(
        [portfolio_returns.rename("portfolio"), benchmark_returns.rename("benchmark")],
        axis=1,
        join="inner",
    ).replace([np.inf, -np.inf], np.nan).dropna()
    observations = len(aligned)
    result = {
        "value": None,
        "observations": observations,
        "start_date": str(pd.Timestamp(aligned.index[0]).date()) if observations else None,
        "end_date": str(pd.Timestamp(aligned.index[-1]).date()) if observations else None,
        "status": "insufficient_data",
        "method": "historical_covariance_local_currency_approximation",
    }
    if observations < min_observations:
        return result
    variance = float(aligned["benchmark"].var())
    if not math.isfinite(variance) or variance <= 0:
        result["status"] = "zero_benchmark_variance"
        return result
    covariance = float(aligned["portfolio"].cov(aligned["benchmark"]))
    value = covariance / variance
    if math.isfinite(value):
        result.update({"value": value, "status": "actual"})
    return result


def drawdown_score(drawdown_pct: float | None) -> int | None:
    """Score negative drawdown by absolute severity; missing data remains missing."""
    value = _safe_float(drawdown_pct)
    if value is None:
        return None
    severity = abs(min(value, 0.0))
    if severity < 10:
        return 0
    if severity < 20:
        return 5
    if severity < 30:
        return 9
    if severity < 45:
        return 13
    return 15


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
    return_model: "PortfolioReturnModel | None" = None,
) -> dict[str, Any]:
    """计算组合指标。

    §22 修复：可选接收统一 Return Model，从中读取年化波动率、Beta、回撤、风险贡献。
    当 return_model 提供时，优先使用其值。
    """
    holdings = [h for h in snapshot.get("holdings", []) if _safe_float(h.get("weight")) and _safe_float(h.get("weight")) > 0]
    weights_array = np.array([float(h["weight"]) for h in holdings], dtype=float)
    tickers = [h["ticker"] for h in holdings]
    top_weights = sorted(weights_array.tolist(), reverse=True)
    hhi = float(np.sum(weights_array ** 2)) if len(weights_array) else None
    metrics: dict[str, Any] = {
        "top1_weight": top_weights[0] if top_weights else None,
        "top3_weight": float(sum(top_weights[:3])) if top_weights else None,
        "hhi": hhi,
        "hhi_10000": hhi * 10000.0 if hhi is not None else None,
        "effective_holdings": 1.0 / hhi if hhi else None,
        "portfolio_beta": None,
        "portfolio_beta_historical": None,
        "portfolio_beta_weighted_snapshot": None,
        "portfolio_beta_status": "insufficient_data",
        "portfolio_beta_source": None,
        "portfolio_beta_method": None,
        "portfolio_beta_observations": 0,
        "portfolio_beta_start_date": None,
        "portfolio_beta_end_date": None,
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

    # §22 修复：优先使用统一 Return Model
    if return_model is not None:
        if return_model.annualized_volatility is not None:
            metrics["annualized_volatility"] = return_model.annualized_volatility
        if return_model.portfolio_beta is not None:
            metrics["portfolio_beta"] = return_model.portfolio_beta
            metrics["portfolio_beta_historical"] = {
                "value": return_model.portfolio_beta,
                "observations": return_model.beta_observations,
                "start_date": return_model.beta_start_date,
                "end_date": return_model.beta_end_date,
                "status": "actual",
                "method": "return_model_historical_covariance",
            }
            metrics["portfolio_beta_status"] = "actual"
            metrics["portfolio_beta_source"] = "return_model"
            metrics["portfolio_beta_method"] = "historical_covariance_common_dates"
            metrics["portfolio_beta_observations"] = return_model.beta_observations
            metrics["portfolio_beta_start_date"] = return_model.beta_start_date
            metrics["portfolio_beta_end_date"] = return_model.beta_end_date
        if return_model.max_drawdown_63d is not None:
            metrics["max_drawdown_63d"] = return_model.max_drawdown_63d
        if return_model.max_drawdown_252d is not None:
            metrics["max_drawdown_252d"] = return_model.max_drawdown_252d
        # 风险贡献、Scenario 与 Overview 共用 Return Model 的年化 covariance。
        if return_model.covariance_matrix is not None:
            from portfolio_analysis.return_model import risk_contributions as rc_from_model
            weights_dict = {h["ticker"]: float(h.get("weight") or 0.0) for h in holdings}
            metrics["risk_contributions"] = rc_from_model(return_model, weights_dict)
            metrics["covariance_tickers"] = list(return_model.covariance_matrix.index)
            metrics["covariance_matrix_annualized"] = [
                [float(value) if math.isfinite(float(value)) else None for value in row]
                for row in return_model.covariance_matrix.to_numpy()
            ]
            metrics["covariance_frequency"] = return_model.covariance_frequency
            metrics["covariance_source"] = "return_model"
            metrics["covariance_weight_coverage"] = return_model.covariance_weight_coverage
            metrics["covariance_observations"] = return_model.covariance_observations
            metrics["covariance_excluded_tickers"] = list(return_model.covariance_excluded_tickers)

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
        # P0-7: 仅在 return_model 未提供时使用旧计算
        if return_model is None:
            metrics["annualized_volatility"] = float(portfolio_returns.std() * math.sqrt(252) * 100.0)
            metrics["max_drawdown_63d"] = _max_drawdown(portfolio_returns, 63)
            metrics["max_drawdown_252d"] = _max_drawdown(portfolio_returns, 252)

    # 相对收益（修改计划第三轮 4）：使用基准有效日期与共同有效起止日，
    # 不再用 `if b0:` 这类对 NaN 为真值的判定。
    bench_series = close[benchmark].dropna() if benchmark in close.columns else pd.Series(dtype=float)
    if not bench_series.empty:
        benchmark_returns = bench_series.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
        beta_result = calculate_portfolio_beta(portfolio_returns, benchmark_returns)
        # P0-7: 仅在 return_model 未提供时使用旧 beta 计算
        if return_model is None:
            metrics["portfolio_beta"] = beta_result["value"]
            metrics["portfolio_beta_historical"] = beta_result
            metrics["portfolio_beta_status"] = beta_result["status"]
            metrics["portfolio_beta_source"] = "legacy_returns"
            metrics["portfolio_beta_method"] = beta_result.get("method")
            metrics["portfolio_beta_observations"] = beta_result["observations"]
            metrics["portfolio_beta_start_date"] = beta_result.get("start_date")
            metrics["portfolio_beta_end_date"] = beta_result.get("end_date")
        for name, periods in RETURN_WINDOWS.items():
            if len(close) > periods + 1:
                metrics["relative_returns"][name] = calculate_relative_window_return(
                    close[ticker_cols], aligned_weights, bench_series, periods,
                    minimum_weight_coverage=0.90,
                )

    # §17: 简单 Return Contribution（static_weight × ticker_window_return）
    if close_prices is not None and not close_prices.empty and ticker_cols:
        contrib_periods = 5  # 5 日窗口
        if len(close_prices) > contrib_periods:
            ticker_ret = close_prices[ticker_cols].iloc[-contrib_periods:].pct_change(
                fill_method=None
            ).replace([np.inf, -np.inf], np.nan).dropna()
            if len(ticker_ret) > 0:
                contribs = []
                for h in holdings:
                    t = h["ticker"]
                    w = float(h.get("weight") or 0.0)
                    if t in ticker_ret.columns:
                        cum_ret = float((1 + ticker_ret[t]).prod() - 1)
                        contribs.append({
                            "ticker": t,
                            "static_weight": round(w, 4),
                            f"return_{contrib_periods}d": round(cum_ret * 100, 2),
                            "contribution_pct_points": round(w * cum_ret * 100, 4),
                        })
                contribs.sort(key=lambda x: x["contribution_pct_points"])
                metrics["relative_return_contributions"] = contribs

    holding_betas = [
        (float(h["weight"]), _safe_float(h.get("beta")))
        for h in holdings if _safe_float(h.get("beta")) is not None
    ]
    covered_beta_weight = sum(weight for weight, _ in holding_betas)
    if covered_beta_weight > 0:
        metrics["portfolio_beta_weighted_snapshot"] = {
            "value": sum(weight * float(beta) for weight, beta in holding_betas) / covered_beta_weight,
            "weight_coverage": covered_beta_weight,
            "status": "approximate",
        }

    corr_source = returns[ticker_cols].dropna(how="all")
    corr = corr_source.corr(min_periods=60)
    if corr.shape[0] >= 2:
        mask = np.triu(np.ones(corr.shape), k=1).astype(bool)
        values = corr.where(mask).stack().dropna()
        if not values.empty:
            metrics["average_pairwise_correlation"] = float(values.mean())
            metrics["max_pairwise_correlation"] = float(values.max())
            metrics["high_correlation_pairs"] = [
                {
                    "ticker_a": a,
                    "ticker_b": b,
                    "correlation": float(v),
                    "observations": int(corr_source[[a, b]].dropna().shape[0]),
                    "combined_weight": float(aligned_weights.get(a, 0.0) + aligned_weights.get(b, 0.0)),
                    "pair_exposure": float(min(aligned_weights.get(a, 0.0), aligned_weights.get(b, 0.0)) * max(0.0, float(v))),
                }
                for (a, b), v in values.sort_values(ascending=False).head(5).items()
                if v >= 0.75
            ]
            metrics["weighted_high_correlation_exposure"] = float(sum(
                pair["pair_exposure"] for pair in metrics["high_correlation_pairs"]
            ))

    cov = returns[ticker_cols].dropna(how="all").cov()
    if cov.shape[0] and aligned_weights.sum() > 0 and return_model is None:
        metrics["covariance_tickers"] = list(cov.index)
        metrics["covariance_matrix_daily"] = [
            [float(value) if math.isfinite(float(value)) else None for value in row]
            for row in cov.to_numpy()
        ]
        metrics["covariance_frequency"] = "daily"
        metrics["covariance_source"] = "legacy_returns"
        # 旧调用方没有 Return Model 时才使用旧风险贡献计算。
        if return_model is None:
            w = aligned_weights.reindex(cov.index).fillna(0.0).to_numpy()
            cov_matrix = cov.to_numpy()
            variance = float(w.T @ cov_matrix @ w)
            if variance > 0:
                volatility = math.sqrt(variance)
                marginal = cov_matrix @ w / volatility
                component = w * marginal
                total_component = float(component.sum())
                if total_component != 0:
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
    _technical_breadth_from_snapshot(metrics, holdings)
    metrics["aggregates"] = _compute_aggregates(holdings, metrics)

    # §24: metadata_coverage 供风险评分置信度使用
    inst_meta = snapshot.get("instrument_metadata", {}) if isinstance(snapshot, dict) else {}
    tickers_held = {h["ticker"] for h in holdings}
    metrics["metadata_coverage_score"] = round(
        sum(1 for t in tickers_held if inst_meta.get(t, {}).get("instrument_type"))
        / max(1, len(tickers_held)), 3
    ) if tickers_held else 0.8

    # 修改计划第三轮 22：Python 风险评分，AI 只负责解释（最多凭新鲜证据上调一级）。
    risk_score = compute_portfolio_risk_score(metrics)
    metrics["portfolio_risk_score"] = risk_score["score"]
    metrics["portfolio_risk_level"] = risk_score["level"]
    metrics["risk_score_components"] = risk_score["components"]
    metrics["risk_score_component_max"] = risk_score["component_max"]
    metrics["risk_score_available_components"] = risk_score["available_components"]
    metrics["risk_score_missing_components"] = risk_score["missing_components"]
    metrics["risk_score_confidence"] = risk_score["score_confidence"]
    return metrics


def _compute_aggregates(holdings: list[dict[str, Any]], metrics: dict[str, Any]) -> dict[str, Any]:
    """修改计划第三轮 20：所有聚合数字由 Python 预计算，模型只能引用，不得自行算术。"""
    top_risk = sorted(
        metrics.get("risk_contributions", []) or [],
        key=lambda x: -(float(x.get("risk_contribution") or 0.0)),
    )[:5]
    top_risk_contribution_sum = float(sum(float(x.get("risk_contribution") or 0.0) for x in top_risk))
    top_risk_weight_sum = float(sum(float(x.get("weight") or 0.0) for x in top_risk))
    beta_gt_1_5_weight = float(sum(
        float(h.get("weight") or 0.0)
        for h in holdings
        if (_safe_float(h.get("beta")) or 0.0) > 1.5
    ))
    beta_gt_2_0_weight = float(sum(
        float(h.get("weight") or 0.0)
        for h in holdings
        if (_safe_float(h.get("beta")) or 0.0) > 2.0
    ))
    below_ema50_weight = float(sum(
        float(h.get("weight") or 0.0)
        for h in holdings
        if _safe_float(h.get("price_vs_ema50_pct")) is not None and _safe_float(h.get("price_vs_ema50_pct")) < 0
    ))
    holding_by_ticker = {str(h.get("ticker")): h for h in holdings}
    top5_members = [str(x.get("ticker")) for x in top_risk if x.get("ticker")]
    top5_below_ema50 = sum(
        1 for ticker in top5_members
        if _safe_float((holding_by_ticker.get(ticker) or {}).get("price_vs_ema50_pct")) is not None
        and _safe_float((holding_by_ticker.get(ticker) or {}).get("price_vs_ema50_pct")) < 0
    )
    return {
        "top_risk_contribution_sum": round(top_risk_contribution_sum, 4),
        "top_risk_weight_sum": round(top_risk_weight_sum, 4),
        "top5_risk_contributors": [
            {
                "ticker": x.get("ticker"),
                "risk_contribution": round(float(x.get("risk_contribution") or 0.0), 6),
                "weight": round(float(x.get("weight") or 0.0), 6),
            }
            for x in top_risk
        ],
        "top5_risk_contributor_tickers": top5_members,
        "top5_below_ema50_count": top5_below_ema50,
        "top5_count": len(top5_members),
        "high_beta_weight": {"threshold": 1.5, "weight": round(beta_gt_1_5_weight, 4)},
        "beta_gt_1_5_weight": round(beta_gt_1_5_weight, 4),
        "beta_gt_2_0_weight": round(beta_gt_2_0_weight, 4),
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
    corr_exposure = _safe_float(metrics.get("weighted_high_correlation_exposure"))
    breadth = _safe_float((metrics.get("technical_breadth", {}) or {}).get("below_ema50_weight"))
    top_risk_sum = _safe_float((metrics.get("aggregates", {}) or {}).get("top_risk_contribution_sum"))

    def optional_band(value: float | None, bands: list[tuple[float, int]]) -> int | None:
        return None if value is None else _score_band(value, bands)

    raw_components = {
        "concentration": optional_band(top1, [(0.05, 0), (0.10, 4), (0.20, 9), (0.30, 13), (1e9, 15)]),
        "beta": optional_band(beta, [(0.8, 0), (1.0, 3), (1.25, 7), (1.5, 11), (1e9, 15)]),
        "volatility": optional_band(vol, [(10, 0), (15, 5), (20, 9), (30, 13), (1e9, 15)]),
        "drawdown": drawdown_score(dd),
        "correlation": optional_band(corr_exposure, [(0.01, 0), (0.03, 3), (0.07, 6), (0.12, 8), (1e9, 10)]),
        "breadth": optional_band(breadth, [(0.3, 0), (0.5, 4), (0.7, 9), (0.85, 13), (1e9, 15)]),
        "risk_contribution": optional_band(top_risk_sum, [(0.3, 0), (0.5, 4), (0.65, 9), (0.8, 13), (1e9, 15)]),
    }
    missing = [name for name, value in raw_components.items() if value is None]
    available = {name: value for name, value in raw_components.items() if value is not None}
    available_max = sum(RISK_COMPONENT_WEIGHTS[name] for name in available)
    score = int(round(sum(float(v) for v in available.values()) / available_max * 100.0)) if available_max else 0
    components = {name: raw_components[name] for name in RISK_COMPONENT_WEIGHTS}
    if score < 30:
        level = "low"
    elif score < 50:
        level = "medium"
    elif score < 70:
        level = "medium_high"
    else:
        level = "high"
    # §24 修复：风险评分置信度不能仅基于组件可用性，需考虑数据质量
    component_availability = round(available_max / 100.0, 3)
    metadata_cov = metrics.get("metadata_coverage_score", 1.0)
    if metadata_cov is None:
        metadata_cov = 1.0
    perf_method = metrics.get("performance_methodology", {})
    fx_aligned = 1.0 if perf_method.get("historical_fx_aligned", False) else 0.85
    score_confidence = round(min(component_availability, metadata_cov, fx_aligned), 3)
    return {
        "score": score,
        "level": level,
        "components": components,
        "component_max": dict(RISK_COMPONENT_WEIGHTS),
        "available_components": len(available),
        "missing_components": missing,
        "score_confidence": score_confidence,
    }


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
    above_ema50 = weight_sum(lambda h: _safe_float(h.get("price_vs_ema50_pct")) is not None and _safe_float(h.get("price_vs_ema50_pct")) >= 0)
    metrics["technical_breadth"] = {
        "above_ema20_weight": weight_sum(lambda h: _safe_float(h.get("price_vs_ema20_pct")) is not None and _safe_float(h.get("price_vs_ema20_pct")) >= 0),
        "above_ema50_weight": above_ema50,
        "below_ema50_weight": (1.0 - above_ema50) if above_ema50 is not None else None,
        "above_ema200_weight": weight_sum(lambda h: _safe_float(h.get("price_vs_ema200_pct")) is not None and _safe_float(h.get("price_vs_ema200_pct")) >= 0),
        "rsi_over_70_weight": weight_sum(lambda h: _safe_float(h.get("rsi")) is not None and _safe_float(h.get("rsi")) > 70),
        "rsi_under_30_weight": weight_sum(lambda h: _safe_float(h.get("rsi")) is not None and _safe_float(h.get("rsi")) < 30),
    }
