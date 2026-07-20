# -*- coding: utf-8 -*-
"""统一 Portfolio Return Model。

本模块是组合收益、波动率、Beta、风险贡献、Scenario 和累计收益的唯一口径。
协方差矩阵统一保存为年化矩阵，所有下游不得再次乘以 252。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class PortfolioReturnModel:
    """统一 Portfolio 收益模型输出。"""

    daily_returns: pd.Series
    valid_dates: list[str]
    daily_weight_coverage: pd.Series
    covariance_matrix: pd.DataFrame | None
    annualized_volatility: float | None
    max_drawdown_63d: float | None
    max_drawdown_252d: float | None
    portfolio_beta: float | None
    cumulative_returns: pd.Series
    benchmark_cumulative_returns: pd.Series
    invalid_date_count: int
    total_date_count: int
    beta_observations: int = 0
    beta_start_date: str | None = None
    beta_end_date: str | None = None
    covariance_frequency: str = "annualized"
    base_weights: dict[str, float] = field(default_factory=dict)
    covariance_weight_coverage: float = 0.0
    covariance_excluded_tickers: list[str] = field(default_factory=list)
    covariance_observations: int = 0


def _empty_model() -> PortfolioReturnModel:
    empty = pd.Series(dtype=float)
    return PortfolioReturnModel(
        daily_returns=empty.copy(),
        valid_dates=[],
        daily_weight_coverage=empty.copy(),
        covariance_matrix=None,
        annualized_volatility=None,
        max_drawdown_63d=None,
        max_drawdown_252d=None,
        portfolio_beta=None,
        cumulative_returns=empty.copy(),
        benchmark_cumulative_returns=empty.copy(),
        invalid_date_count=0,
        total_date_count=0,
        beta_observations=0,
        beta_start_date=None,
        beta_end_date=None,
        covariance_frequency="annualized",
        base_weights={},
        covariance_weight_coverage=0.0,
        covariance_excluded_tickers=[],
        covariance_observations=0,
    )


def build_portfolio_return_model(
    close: pd.DataFrame,
    weights: dict[str, float],
    *,
    benchmark: str = "^GSPC",
    min_weight_coverage: float = 0.90,
    window_252: int = 252,
    window_63: int = 63,
) -> PortfolioReturnModel:
    """构建统一 Portfolio Return Model。

    每个交易日只有在有效权重覆盖达到 ``min_weight_coverage`` 时才进入组合收益。
    协方差矩阵在同一有效日期集合上计算并年化，Overview、Risk Contribution 与
    Action Scenario 共用该矩阵。
    """
    if close is None or close.empty:
        return _empty_model()

    tickers = [t for t in weights if t in close.columns and float(weights[t] or 0.0) > 0]
    if not tickers:
        return _empty_model()

    weight_series = pd.Series({t: float(weights[t]) for t in tickers}, dtype=float)
    total_weight = float(weight_series.sum())
    if total_weight <= 0:
        return _empty_model()
    weight_series = weight_series / total_weight

    returns = close[tickers].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
    valid_mask = returns.notna()
    daily_available_weight = valid_mask.mul(weight_series, axis=1).sum(axis=1)
    daily_weight_coverage = daily_available_weight.copy()

    weighted_returns = returns.mul(weight_series, axis=1).sum(axis=1, min_count=1)
    valid_dates_mask = daily_weight_coverage >= float(min_weight_coverage)
    portfolio_raw = weighted_returns / daily_available_weight.replace(0, np.nan)
    portfolio_with_invalid = portfolio_raw.where(valid_dates_mask)

    invalid_count = int(portfolio_with_invalid.isna().sum())
    total_count = int(len(portfolio_with_invalid))
    portfolio_returns = portfolio_with_invalid.dropna()
    cumulative = (1.0 + portfolio_returns).cumprod() - 1.0

    def _max_drawdown(series: pd.Series, window: int) -> float | None:
        if len(series) < 2:
            return None
        recent = series.tail(window) if len(series) > window else series
        cum = (1.0 + recent).cumprod()
        drawdown = (cum / cum.cummax() - 1.0) * 100.0
        return float(drawdown.min()) if not drawdown.empty else None

    dd_63 = _max_drawdown(portfolio_returns, window_63)
    dd_252 = _max_drawdown(portfolio_returns, window_252)

    # 在组合有效日期集合上计算年化协方差；下游不得再次乘以 252。
    #
    # 极短历史的微小持仓不能让整张矩阵失效。先排除收益观测不足的
    # ticker，再逐步移除造成 complete-case 样本不足的最低覆盖标的。
    # 只有保留权重达到 min_weight_coverage 时才发布协方差风险模型。
    cov_matrix: pd.DataFrame | None = None
    ann_vol: float | None = None
    covariance_weight_coverage = 0.0
    covariance_observations = 0
    cov_source = returns.reindex(portfolio_returns.index)
    min_cov_observations = 20
    cov_tickers = [
        ticker for ticker in tickers
        if int(cov_source[ticker].notna().sum()) >= min_cov_observations
    ]
    while cov_tickers:
        complete = cov_source[cov_tickers].dropna(how="any")
        retained_weight = float(weight_series.reindex(cov_tickers).sum())
        if len(complete) >= 30 and retained_weight >= float(min_weight_coverage):
            candidate = complete.cov() * 252.0
            candidate = candidate.reindex(index=cov_tickers, columns=cov_tickers)
            if candidate.notna().all().all() and np.isfinite(candidate.to_numpy(dtype=float)).all():
                raw_w = weight_series.reindex(cov_tickers).to_numpy(dtype=float)
                variance = float(raw_w.T @ candidate.to_numpy(dtype=float) @ raw_w)
                if np.isfinite(variance) and variance >= 0:
                    cov_matrix = candidate
                    covariance_weight_coverage = retained_weight
                    covariance_observations = int(len(complete))
                    ann_vol = float(np.sqrt(variance) * 100.0)
                    break
        # 移除有效观测最少的标的；并列时优先移除权重最低者。
        drop_ticker = min(
            cov_tickers,
            key=lambda ticker: (
                int(cov_source[ticker].notna().sum()),
                float(weight_series.get(ticker, 0.0)),
            ),
        )
        cov_tickers.remove(drop_ticker)

    covariance_excluded_tickers = [ticker for ticker in tickers if ticker not in cov_tickers]

    # 样本不足时才退回组合收益序列的历史标准差。
    if ann_vol is None and len(portfolio_returns) >= 20:
        ann_vol = float(portfolio_returns.std(ddof=1) * np.sqrt(252) * 100.0)

    beta: float | None = None
    beta_observations = 0
    beta_start_date = None
    beta_end_date = None
    bench_cum = pd.Series(dtype=float)
    if benchmark in close.columns:
        bench_ret = close[benchmark].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
        aligned = portfolio_returns.to_frame("portfolio").join(
            bench_ret.to_frame("benchmark"), how="inner"
        ).dropna()
        beta_observations = int(len(aligned))
        if beta_observations:
            beta_start_date = str(pd.Timestamp(aligned.index[0]).date())
            beta_end_date = str(pd.Timestamp(aligned.index[-1]).date())
            bench_cum = (1.0 + aligned["benchmark"]).cumprod() - 1.0
        if beta_observations >= 30:
            variance = float(aligned["benchmark"].var(ddof=1))
            covariance = float(aligned["portfolio"].cov(aligned["benchmark"]))
            if np.isfinite(variance) and variance > 0 and np.isfinite(covariance):
                beta = covariance / variance

    return PortfolioReturnModel(
        daily_returns=portfolio_returns,
        valid_dates=[str(pd.Timestamp(d).date()) for d in portfolio_returns.index],
        daily_weight_coverage=daily_weight_coverage,
        covariance_matrix=cov_matrix,
        annualized_volatility=ann_vol,
        max_drawdown_63d=dd_63,
        max_drawdown_252d=dd_252,
        portfolio_beta=beta,
        cumulative_returns=cumulative,
        benchmark_cumulative_returns=bench_cum,
        invalid_date_count=invalid_count,
        total_date_count=total_count,
        beta_observations=beta_observations,
        beta_start_date=beta_start_date,
        beta_end_date=beta_end_date,
        covariance_frequency="annualized",
        base_weights={t: float(weight_series[t]) for t in (list(cov_matrix.index) if cov_matrix is not None else tickers)},
        covariance_weight_coverage=float(covariance_weight_coverage),
        covariance_excluded_tickers=covariance_excluded_tickers,
        covariance_observations=covariance_observations,
    )


def scenario_volatility(
    model: PortfolioReturnModel,
    current_weights: dict[str, float],
    target_weights: dict[str, float],
) -> dict[str, Any]:
    """使用 Return Model 的年化协方差计算当前与目标组合风险。"""
    if model.covariance_matrix is None:
        return {
            "current_volatility": model.annualized_volatility,
            "target_volatility": None,
            "volatility_reduction_pct_points": None,
            "variance_reduction_ratio": None,
            "overview_volatility_check": model.annualized_volatility is None,
            "method": "return_model_covariance_unavailable",
        }

    cov = model.covariance_matrix
    tickers = [t for t in cov.index if t in current_weights]
    if not tickers:
        return {
            "current_volatility": model.annualized_volatility,
            "target_volatility": None,
            "volatility_reduction_pct_points": None,
            "variance_reduction_ratio": None,
            "overview_volatility_check": model.annualized_volatility is None,
            "method": "return_model_no_common_tickers",
        }

    cur_vec = np.array([float(model.base_weights.get(t, current_weights.get(t, 0.0))) for t in tickers], dtype=float)
    tgt_vec = np.array([float(target_weights.get(t, model.base_weights.get(t, 0.0))) for t in tickers], dtype=float)
    cov_mat = cov.loc[tickers, tickers].to_numpy(dtype=float)
    cur_var = float(cur_vec @ cov_mat @ cur_vec)
    tgt_var = float(tgt_vec @ cov_mat @ tgt_vec)
    if not (np.isfinite(cur_var) and np.isfinite(tgt_var)) or cur_var < 0 or tgt_var < 0:
        return {
            "current_volatility": model.annualized_volatility,
            "target_volatility": None,
            "volatility_reduction_pct_points": None,
            "variance_reduction_ratio": None,
            "overview_volatility_check": False,
            "method": "return_model_invalid_variance",
        }

    cur_vol = float(np.sqrt(cur_var) * 100.0)
    tgt_vol = float(np.sqrt(tgt_var) * 100.0)
    overview = model.annualized_volatility
    return {
        "current_volatility": round(cur_vol, 4),
        "target_volatility": round(tgt_vol, 4),
        "volatility_reduction_pct_points": round(max(0.0, cur_vol - tgt_vol), 4),
        "variance_reduction_ratio": round(max(0.0, 1.0 - tgt_var / cur_var), 6) if cur_var > 0 else 0.0,
        "overview_volatility_check": overview is not None and abs(cur_vol - overview) <= 1e-6,
        "method": "return_model_annualized_covariance",
    }


def risk_contributions(
    model: PortfolioReturnModel,
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    """计算正风险预算贡献，同时保留带符号的原始边际贡献。"""
    if model.covariance_matrix is None:
        return []
    cov = model.covariance_matrix
    tickers = [t for t in cov.index if float(weights.get(t, 0.0) or 0.0) > 0]
    if not tickers:
        return []

    w = np.array([float(weights[t]) for t in tickers], dtype=float)
    cov_mat = cov.loc[tickers, tickers].to_numpy(dtype=float)
    port_var = float(w @ cov_mat @ w)
    if not np.isfinite(port_var) or port_var <= 0:
        return []

    signed = w * (cov_mat @ w) / port_var
    positive = np.clip(signed, 0.0, None)
    positive_sum = float(positive.sum())
    normalized = positive / positive_sum if positive_sum > 0 else np.zeros_like(positive)
    return [
        {
            "ticker": ticker,
            "weight": float(weights[ticker]),
            "risk_contribution": float(normalized[i]),
            "risk_contribution_pct": float(normalized[i]),
            "signed_risk_contribution": float(signed[i]),
            "risk_weight_gap": float(normalized[i] - float(weights[ticker])),
            "method": "positive_marginal_variance_contribution_normalized",
        }
        for i, ticker in enumerate(tickers)
    ]
