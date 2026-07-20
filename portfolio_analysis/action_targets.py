"""Deterministic portfolio action targets and Return-Model risk estimates."""
from __future__ import annotations

from typing import Any

import math
import numpy as np

from portfolio_analysis.return_model import PortfolioReturnModel, scenario_volatility


def calculate_risk_budget_target_range(
    current_weight: float,
    risk_contribution: float | None,
    *,
    action: str,
    max_position_weight: float = 0.20,
) -> dict[str, Any]:
    current = max(0.0, float(current_weight or 0.0))
    rc = float(risk_contribution) if risk_contribution is not None else current
    ratio = rc / current if current > 0 else 1.0
    action = str(action or "watch").lower()
    if action in {"watch", "hold"}:
        lo = hi = current
    elif action == "add":
        lo = current
        hi = min(max_position_weight, max(current * 1.10, current + 0.005))
    else:
        severity = min(0.60, max(0.10, (ratio - 1.0) * 0.25 + (0.20 if action == "reduce" else 0.10)))
        midpoint = max(0.0, current * (1.0 - severity))
        width = min(0.01, current * 0.10)
        lo = max(0.0, midpoint - width)
        hi = min(current * 0.95, midpoint + width)
        if action == "reduce":
            hi = min(hi, current * 0.80)
            lo = min(lo, hi)
    return {
        "recommended_target_weight_min": round(lo, 6),
        "recommended_target_weight_max": round(max(lo, hi), 6),
        "method": "risk_budget_equalization",
        "risk_contribution_to_weight_ratio": round(ratio, 4),
    }


def apply_deterministic_action_targets(
    advice: dict[str, Any],
    metrics: dict[str, Any],
    settings: dict[str, Any],
    *,
    return_model: PortfolioReturnModel | None = None,
) -> dict[str, Any]:
    rc = {
        str(item.get("ticker")): item.get("risk_contribution")
        for item in metrics.get("risk_contributions", []) or []
    }
    current_weights = {
        str(item.get("ticker")): float(item.get("weight") or 0.0)
        for item in metrics.get("risk_contributions", []) or []
    }
    max_position = float(settings.get("max_position_weight") or settings.get("max_position_pct") or 0.20)
    if max_position > 1:
        max_position /= 100.0

    for item in advice.get("actions") or []:
        ticker = str(item.get("ticker") or "")
        action_type = str(item.get("action") or "watch").lower()
        target = calculate_risk_budget_target_range(
            float(item.get("current_weight") or 0.0),
            rc.get(ticker),
            action=action_type,
            max_position_weight=max_position,
        )
        item["target_weight_min"] = target["recommended_target_weight_min"]
        item["target_weight_max"] = target["recommended_target_weight_max"]
        item["target_weight_method"] = target["method"]
        item["expected_portfolio_risk_reduction"] = None
        item["expected_risk_change"] = None

        if action_type in {"watch", "hold"}:
            continue

        target_weights = dict(current_weights)
        target_weights[ticker] = (
            target["recommended_target_weight_min"] + target["recommended_target_weight_max"]
        ) / 2.0
        if return_model is not None:
            change = scenario_volatility(return_model, current_weights, target_weights)
            if change.get("target_volatility") is None:
                continue
            item["expected_risk_change"] = change
            item["expected_portfolio_risk_reduction"] = change.get("variance_reduction_ratio")
            continue

        # 旧调用方兼容：仅在没有 Return Model 时读取明确标记为 daily 的 legacy covariance。
        covariance_tickers = list(metrics.get("covariance_tickers") or [])
        covariance_raw = metrics.get("covariance_matrix_daily") or []
        if ticker not in covariance_tickers:
            continue
        try:
            covariance = np.asarray(covariance_raw, dtype=float)
        except (TypeError, ValueError):
            continue
        if covariance.shape != (len(covariance_tickers), len(covariance_tickers)) or not np.isfinite(covariance).all():
            continue
        before = np.asarray([current_weights.get(t, 0.0) for t in covariance_tickers], dtype=float)
        after = np.asarray([target_weights.get(t, 0.0) for t in covariance_tickers], dtype=float)
        current_variance = float(before.T @ covariance @ before)
        target_variance = float(after.T @ covariance @ after)
        if current_variance <= 0 or target_variance < 0:
            continue
        current_vol = math.sqrt(current_variance * 252.0) * 100.0
        target_vol = math.sqrt(target_variance * 252.0) * 100.0
        reduction = max(0.0, 1.0 - target_variance / current_variance)
        item["expected_portfolio_risk_reduction"] = round(reduction, 6)
        item["expected_risk_change"] = {
            "method": "target_midpoint_to_cash_same_covariance",
            "current_annualized_volatility": round(current_vol, 4),
            "new_annualized_volatility": round(target_vol, 4),
            "volatility_reduction_pct_points": round(max(0.0, current_vol - target_vol), 4),
            "variance_reduction_ratio": round(reduction, 6),
        }
    return advice


def calculate_reallocation_summary(advice: dict[str, Any]) -> dict[str, Any]:
    reduction = 0.0
    for item in advice.get("actions") or []:
        if item.get("action") not in {"trim", "reduce", "exit"}:
            continue
        current = float(item.get("current_weight") or 0.0)
        lo = float(item.get("target_weight_min") or current)
        hi = float(item.get("target_weight_max") or current)
        reduction += max(0.0, current - (lo + hi) / 2.0)
    return {
        "estimated_weight_reduction": round(reduction, 6),
        "calculation_method": "target_range_midpoint_to_cash",
        "destination": "cash_unspecified",
        "note": "暂留现金，具体再配置未指定。",
    }
