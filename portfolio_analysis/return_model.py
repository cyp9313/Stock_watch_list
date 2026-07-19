# -*- coding: utf-8 -*-
"""统一 Portfolio Return Model（修改计划第六轮第 29 节）。

解决第五轮遗留问题：
    概览年化波动率：13.09%
    Action Scenario 当前年化波动率：17.95%

根因：概览和 scenario 使用不同的 return 计算口径。本模块统一输出：
    - daily portfolio returns
    - valid dates
    - daily weight coverage
    - covariance matrix
    - annualized volatility
    - drawdown
    - beta
    - risk contribution
    - scenario risk
    - cumulative returns

每日权重覆盖规则（修改计划第 29 节）：
    available_weight = sum(weights for tickers with valid return)
    if available_weight >= 0.90:
        portfolio_return = weighted_sum / available_weight
    else:
        invalid_date  # 该日不纳入计算

所有模块（概览、scenario、risk contribution）必须共用本模块的输出。
"""
from __future__ import annotations

from dataclasses import dataclass
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
    benchmark_cumulative_returns: pd.Series  # P0-8
    invalid_date_count: int
    total_date_count: int


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

    Args:
        close: 含各 ticker + benchmark 列的收盘价 DataFrame
        weights: ticker -> 权重
        benchmark: 基准列名
        min_weight_coverage: 当日有效权重占比下限（默认 0.90）
        window_252: 252 日窗口
        window_63: 63 日窗口

    Returns:
        PortfolioReturnModel，所有下游模块共用。
    """
    # 仅保留有权重的 ticker
    tickers = [t for t in weights if t in close.columns and weights[t] > 0]
    if not tickers:
        return PortfolioReturnModel(
            daily_returns=pd.Series(dtype=float),
            valid_dates=[],
            daily_weight_coverage=pd.Series(dtype=float),
            covariance_matrix=None,
            annualized_volatility=None,
            max_drawdown_63d=None,
            max_drawdown_252d=None,
            portfolio_beta=None,
            cumulative_returns=pd.Series(dtype=float),
            invalid_date_count=0,
            total_date_count=0,
        )

    returns = close[tickers].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
    weight_series = pd.Series({t: float(weights[t]) for t in tickers})
    total_weight = float(weight_series.sum())

    # 每日有效权重覆盖（修改计划第 29 节）
    valid_mask = returns.notna()
    daily_available_weight = valid_mask.mul(weight_series, axis=1).sum(axis=1)
    daily_weight_coverage = (daily_available_weight / total_weight) if total_weight > 0 else daily_available_weight

    # 每日组合收益：available_weight >= min_weight_coverage 时才计算
    # portfolio_return = weighted_sum / available_weight（归一化到有效权重）
    weighted_returns = returns.mul(weight_series, axis=1).sum(axis=1, min_count=1)
    valid_dates_mask = daily_weight_coverage >= min_weight_coverage
    # 归一化：除以当日有效权重，避免缺失 ticker 导致收益偏低
    portfolio_raw = weighted_returns / daily_available_weight.replace(0, np.nan)
    portfolio_returns = portfolio_raw.where(valid_dates_mask)

    # 清理无效日
    invalid_count = int(portfolio_returns.isna().sum())
    total_count = int(len(portfolio_returns))
    portfolio_returns = portfolio_returns.dropna()

    # 累计收益
    cumulative = ((1 + portfolio_returns).cumprod() - 1)

    # 年化波动率（252 日窗口）
    ann_vol: float | None = None
    if len(portfolio_returns) >= 20:
        ann_vol = float(portfolio_returns.std(ddof=1) * np.sqrt(252) * 100.0)

    # 回撤
    def _max_drawdown(series: pd.Series, window: int) -> float | None:
        if len(series) < 2:
            return None
        recent = series.tail(window) if len(series) > window else series
        cum = (1 + recent).cumprod()
        rolling_max = cum.expanding().max()
        drawdown = (cum / rolling_max - 1) * 100.0
        return float(drawdown.min()) if not drawdown.empty else None

    dd_63 = _max_drawdown(portfolio_returns, window_63)
    dd_252 = _max_drawdown(portfolio_returns, window_252)

    # Beta（相对 benchmark）
    beta: float | None = None
    bench_cum: pd.Series = pd.Series(dtype=float)
    if benchmark in close.columns:
        bench_ret = close[benchmark].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
        aligned = portfolio_returns.to_frame("port").join(bench_ret.to_frame("bench"), how="inner").dropna()
        if len(aligned) >= 30:
            cov = float(aligned["port"].cov(aligned["bench"]))
            var = float(aligned["bench"].var(ddof=1))
            if var > 0:
                beta = cov / var
        # P0-8: benchmark cumulative returns（对齐到共同日期）
        bench_cum = ((1 + bench_ret).cumprod() - 1)
        bench_cum = bench_cum.reindex(portfolio_returns.index).ffill()

    # 协方差矩阵（用于 risk contribution 和 scenario）
    cov_matrix: pd.DataFrame | None = None
    if len(returns) >= 30:
        cov_matrix = returns.cov() * 252.0  # 年化

    return PortfolioReturnModel(
        daily_returns=portfolio_returns,
        valid_dates=[str(d.date()) for d in portfolio_returns.index],
        daily_weight_coverage=daily_weight_coverage.dropna(),
        covariance_matrix=cov_matrix,
        annualized_volatility=ann_vol,
        max_drawdown_63d=dd_63,
        max_drawdown_252d=dd_252,
        portfolio_beta=beta,
        cumulative_returns=cumulative,
        benchmark_cumulative_returns=bench_cum,
        invalid_date_count=invalid_count,
        total_date_count=total_count,
    )


def scenario_volatility(
    model: PortfolioReturnModel,
    current_weights: dict[str, float],
    target_weights: dict[str, float],
) -> dict[str, Any]:
    """计算 scenario 下的组合波动率（复用同一 covariance matrix）。

    修改计划第 29 节验收标准：overview volatility == scenario current volatility。
    """
    if model.covariance_matrix is None:
        return {"current_volatility": None, "target_volatility": None, "volatility_reduction": None}
    cov = model.covariance_matrix
    tickers = [t for t in current_weights if t in cov.index]
    if not tickers:
        return {"current_volatility": None, "target_volatility": None, "volatility_reduction": None}

    cur_vec = np.array([float(current_weights.get(t, 0.0)) for t in tickers])
    tgt_vec = np.array([float(target_weights.get(t, 0.0)) for t in tickers])
    cov_mat = cov.loc[tickers, tickers].values

    cur_var = float(cur_vec @ cov_mat @ cur_vec)
    tgt_var = float(tgt_vec @ cov_mat @ tgt_vec)
    cur_vol = float(np.sqrt(cur_var) * 100.0)
    tgt_vol = float(np.sqrt(tgt_var) * 100.0)
    return {
        "current_volatility": round(cur_vol, 4),
        "target_volatility": round(tgt_vol, 4),
        "volatility_reduction_pct_points": round(max(0.0, cur_vol - tgt_vol), 4),
        "variance_reduction_ratio": round(max(0.0, 1.0 - tgt_var / cur_var), 6) if cur_var > 0 else 0.0,
        # 验收字段：应与 overview annualized_volatility 一致
        "overview_volatility_check": round(cur_vol, 4) == round(model.annualized_volatility or 0, 4),
    }


def risk_contributions(
    model: PortfolioReturnModel,
    weights: dict[str, float],
) -> list[dict[str, Any]]:
    """计算各 ticker 的风险贡献（复用同一 covariance matrix）。"""
    if model.covariance_matrix is None:
        return []
    cov = model.covariance_matrix
    tickers = [t for t in weights if t in cov.index and weights[t] > 0]
    if not tickers:
        return []
    w = np.array([float(weights[t]) for t in tickers])
    cov_mat = cov.loc[tickers, tickers].values
    port_var = float(w @ cov_mat @ w)
    if port_var <= 0:
        return []
    # 边际风险贡献 = (cov @ w) / sqrt(port_var)
    marginal = cov_mat @ w
    rc = w * marginal / port_var
    return [
        {
            "ticker": t,
            "weight": float(weights[t]),
            "risk_contribution": float(rc[i]),
            "risk_contribution_pct": float(rc[i] / rc.sum()) if rc.sum() > 0 else 0.0,
        }
        for i, t in enumerate(tickers)
    ]
