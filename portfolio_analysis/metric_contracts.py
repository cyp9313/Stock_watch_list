# -*- coding: utf-8 -*-
"""Portfolio 指标字段单位契约（修改计划第三轮 2.3）。

背景：数据模型混用了两类口径——
- 0~1 比例（ratio）：weight / risk_contribution / target_weight_min / top1_weight ...
- 已乘 100 的百分数（pct_value）：profit_loss_pct / return_1d / annualized_volatility ...
- 普通无量纲数（number）：portfolio_beta / rsi / hhi_10000 / effective_holdings ...

禁止再「根据绝对值猜单位」。所有展示必须显式选择
``format_ratio_as_pct`` / ``format_pct_value`` / ``format_number``，
并由本契约单一来源驱动（见 ``fmt_metric``）。
"""
from __future__ import annotations

import math
from typing import Any


# unit ∈ {"ratio", "pct_value", "number"}
METRIC_UNITS = {
    # ── 0~1 比例 ──
    "weight": "ratio",
    "risk_contribution": "ratio",
    "risk_weight_gap": "ratio",
    "target_weight_min": "ratio",
    "target_weight_max": "ratio",
    "top1_weight": "ratio",
    "top3_weight": "ratio",
    "hhi": "ratio",                 # 赫芬达尔指数本身 ∈ [0,1]
    "below_ema50_weight": "ratio",
    "below_ema200_weight": "ratio",
    "high_beta_weight": "ratio",
    # ── 已乘 100 的百分数 ──
    "profit_loss_pct": "pct_value",
    "return_1d": "pct_value",
    "return_5d": "pct_value",
    "return_1m": "pct_value",
    "return_ytd": "pct_value",
    "price_vs_ema20_pct": "pct_value",
    "price_vs_ema50_pct": "pct_value",
    "price_vs_ema200_pct": "pct_value",
    "annualized_volatility": "pct_value",
    "max_drawdown_63d": "pct_value",
    "max_drawdown_252d": "pct_value",
    "distance_from_52w_high": "pct_value",
    "relative": "pct_value",
    "portfolio_return": "pct_value",
    "benchmark_return": "pct_value",
    "top_risk_contribution_sum": "ratio",
    "top_risk_weight_sum": "ratio",
    "recommended_reduction_weight": "ratio",
    "expected_portfolio_risk_reduction": "ratio",
    # ── 无量纲数 ──
    "hhi_10000": "number",
    "effective_holdings": "number",
    "portfolio_beta": "number",
    "rsi": "number",
    "volume_ratio": "number",
    "beta": "number",
    "risk_priority_score": "number",
    "confidence": "ratio",          # 0~1 置信度，按百分比展示
    "risk_model_coverage_ratio": "ratio",
}


def unit_of(name: str) -> str:
    return METRIC_UNITS.get(name, "unknown")


def fmt_metric(name: str, value: Any, digits: int | None = None) -> str:
    """按契约选择格式化函数；未知字段默认当作 number。

    digits 不指定时按比例/百分数自动选 2（权重等比例用 2 位小数）。
    """
    from daily_report import report_i18n as _i18n  # 延迟导入，避免包级循环依赖
    unit = METRIC_UNITS.get(name)
    if unit == "ratio":
        return _i18n.format_ratio_as_pct(value, digits if digits is not None else 2)
    if unit == "pct_value":
        return _i18n.format_pct_value(value, digits if digits is not None else 2)
    if unit == "number":
        return _i18n.format_number(value, digits if digits is not None else 2)
    return _i18n.format_number(value, digits if digits is not None else 2)


def is_ratio(name: str) -> bool:
    return METRIC_UNITS.get(name) == "ratio"


def is_pct_value(name: str) -> bool:
    return METRIC_UNITS.get(name) == "pct_value"


def scan_non_finite(obj: Any, path: str = "") -> list[str]:
    """递归扫描数据，返回所有非有限数值（NaN / ±Inf）的点路径（修改计划第三轮 3）。

    字符串等非数值会被忽略；只有可转 float 且非有限的值会被记录。
    """
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{path}.{k}" if path else str(k)
            found.extend(scan_non_finite(v, child))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            found.extend(scan_non_finite(v, f"{path}[{i}]"))
    else:
        try:
            number = float(obj)
        except (TypeError, ValueError):
            return found
        if not math.isfinite(number):
            found.append(path or "root")
    return found


# ── 置信度上限因子（修改计划第三轮 23）────────────────────────
def data_quality_score(non_finite_count: int, anomaly_weight: float) -> float:
    """数据完整度评分：非有限指标越多、异常权重越大，分数越低。"""
    score = 1.0
    score -= min(0.5, 0.08 * max(0, non_finite_count))
    score -= min(0.4, 0.6 * max(0.0, min(1.0, anomaly_weight)))
    return round(max(0.1, score), 3)


def metadata_coverage_score(instrument_metadata: dict[str, dict[str, Any]], tickers: list[str]) -> float:
    """工具类型覆盖度：正确识别类型（非 UNKNOWN）的比例。"""
    if not tickers:
        return 0.5
    recognized = sum(
        1 for t in tickers
        if str((instrument_metadata.get(t) or {}).get("instrument_type") or "UNKNOWN").upper() != "UNKNOWN"
    )
    return round(max(0.1, recognized / len(tickers)), 3)


def evidence_coverage_score(evidence: list[dict[str, Any]], top_risk_tickers: list[str]) -> float:
    """证据覆盖度：Top-risk ticker 被高质量证据覆盖的比例。"""
    if not top_risk_tickers:
        return 0.5
    covered = set()
    for e in evidence:
        t = e.get("ticker")
        if t and str(e.get("source_quality") or "tier_3") != "tier_3":
            covered.add(t)
    ratio = len(set(top_risk_tickers) & covered) / len(top_risk_tickers)
    return round(max(0.1, ratio), 3)


def evidence_freshness_score(evidence: list[dict[str, Any]], fresh_days: int = 45, background_days: int = 180) -> float:
    """证据新鲜度：新鲜/近期证据占比（未知日期降权）。"""
    if not evidence:
        return 0.3
    fresh = 0
    today = _today()
    for e in evidence:
        d = _parse_date(e.get("published_date"))
        if d is None:
            continue
        age = (today - d).days
        if age <= fresh_days:
            fresh += 1
        elif age <= background_days:
            fresh += 0.6
    return round(max(0.1, min(1.0, fresh / len(evidence))), 3)


def _today():
    from datetime import date
    return date.today()


def _parse_date(value):
    if not value:
        return None
    import datetime as _dt
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            return _dt.datetime.strptime(str(value)[:len(_dt.date.today().strftime(fmt)) + 1] if False else str(value), fmt).date()
        except (ValueError, TypeError):
            continue
    # 尝试 ISO 日期前缀
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None

