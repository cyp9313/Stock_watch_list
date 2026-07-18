"""Deterministic portfolio action targets and risk-change estimates."""
from __future__ import annotations

import math
from typing import Any

import numpy as np


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
) -> dict[str, Any]:
    rc = {str(x.get("ticker")): x.get("risk_contribution") for x in metrics.get("risk_contributions", []) or []}
    covariance_tickers = list(metrics.get("covariance_tickers") or [])
    covariance_raw = metrics.get("covariance_matrix_daily") or []
    covariance = None
    try:
        covariance = np.asarray(covariance_raw, dtype=float)
        if covariance.shape != (len(covariance_tickers), len(covariance_tickers)) or not np.isfinite(covariance).all():
            covariance = None
    except (TypeError, ValueError):
        covariance = None
    current_weights = {
        str(item.get("ticker")): float(item.get("weight") or 0.0)
        for item in metrics.get("risk_contributions", []) or []
    }
    max_position = float(settings.get("max_position_weight") or settings.get("max_position_pct") or 0.20)
    if max_position > 1:
        max_position /= 100.0
    for item in advice.get("actions") or []:
        ticker = str(item.get("ticker") or "")
        target = calculate_risk_budget_target_range(
            float(item.get("current_weight") or 0.0), rc.get(ticker),
            action=str(item.get("action") or "watch"), max_position_weight=max_position,
        )
        item["target_weight_min"] = target["recommended_target_weight_min"]
        item["target_weight_max"] = target["recommended_target_weight_max"]
        item["target_weight_method"] = target["method"]
        item["expected_portfolio_risk_reduction"] = None
        item["expected_risk_change"] = None
        if covariance is not None and ticker in covariance_tickers:
            before = np.asarray([current_weights.get(t, 0.0) for t in covariance_tickers], dtype=float)
            after = before.copy()
            after[covariance_tickers.index(ticker)] = (
                target["recommended_target_weight_min"] + target["recommended_target_weight_max"]
            ) / 2.0
            current_variance = float(before.T @ covariance @ before)
            new_variance = float(after.T @ covariance @ after)
            if current_variance > 0 and new_variance >= 0:
                current_vol = math.sqrt(current_variance * 252.0) * 100.0
                new_vol = math.sqrt(new_variance * 252.0) * 100.0
                variance_reduction = max(0.0, 1.0 - new_variance / current_variance)
                item["expected_portfolio_risk_reduction"] = round(variance_reduction, 6)
                item["expected_risk_change"] = {
                    "method": "target_midpoint_to_cash_same_covariance",
                    "current_annualized_volatility": round(current_vol, 4),
                    "new_annualized_volatility": round(new_vol, 4),
                    "volatility_reduction_pct_points": round(max(0.0, current_vol - new_vol), 4),
                    "variance_reduction_ratio": round(variance_reduction, 6),
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
        midpoint = (lo + hi) / 2.0
        reduction += max(0.0, current - midpoint)
    return {
        "estimated_weight_reduction": round(reduction, 6),
        "calculation_method": "target_range_midpoint_to_cash",
        "destination": "cash_unspecified",
        "note": "暂留现金，具体再配置未指定。",
    }
