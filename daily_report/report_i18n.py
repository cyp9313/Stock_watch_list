# -*- coding: utf-8 -*-
"""中文报告国际化与格式化层（Portfolio 与个股日报共用）。

本项目目标要求 Portfolio AI 报告默认使用简体中文。本模块集中管理：
- 操作动作中文名；
- 风险等级中文名；
- 严重程度 / 影响方向 / 时间范围 / 工具类型 中文标签；
- 金额、百分比、数字的安全格式化助手。
"""
from __future__ import annotations

import math
from typing import Any


def finite_float(value: Any) -> float | None:
    """返回有限浮点数；None / 非数字 / NaN / ±Inf 一律返回 None。

    用于阻断 NaN / Inf 进入报告（修改计划第三轮 3）。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ── 操作动作中文名 ─────────────────────────────────────────────
ACTION_LABELS_ZH = {
    "add": "增持",
    "hold": "持有",
    "trim": "适度减仓",
    "reduce": "明显减仓",
    "exit": "退出",
    "watch": "观察",
}

# ── 风险等级中文名 ─────────────────────────────────────────────
RISK_LEVEL_LABELS_ZH = {
    "low": "低",
    "medium": "中等",
    "medium_high": "中高",
    "high": "高",
}

# ── 严重程度中文名 ─────────────────────────────────────────────
SEVERITY_LABELS_ZH = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

# ── 影响方向中文名 ─────────────────────────────────────────────
IMPACT_LABELS_ZH = {
    "positive": "利多",
    "negative": "利空",
    "neutral": "中性",
}

# ── 时间范围中文名 ─────────────────────────────────────────────
HORIZON_LABELS_ZH = {
    "short_term": "短期",
    "medium_term": "中期",
    "long_term": "长期",
}

# ── 工具类型中文名 ─────────────────────────────────────────────
INSTRUMENT_TYPE_LABELS_ZH = {
    "EQUITY": "股票",
    "ETF": "ETF",
    "ETC": "商品 ETC",
    "INDEX": "指数",
    "CRYPTO": "加密资产",
    "FUND": "基金",
    "COMMODITY": "大宗商品",
    "UNKNOWN": "未知",
}

# ── 风险类别中文名（供报告 Section 使用）───────────────────────
RISK_CATEGORY_LABELS_ZH = {
    "concentration": "集中度",
    "volatility": "波动率",
    "beta": "Beta",
    "drawdown": "回撤",
    "correlation": "相关性",
    "risk_contribution": "风险贡献",
    "sector_theme": "行业与主题",
    "breadth": "技术面广度",
    "news": "新闻风险",
    "data_quality": "数据质量",
}


def action_zh(action: Any) -> str:
    return ACTION_LABELS_ZH.get(str(action or "").lower(), str(action or "watch"))


def risk_level_zh(level: Any) -> str:
    return RISK_LEVEL_LABELS_ZH.get(str(level or "").lower(), str(level or "中等"))


def severity_zh(severity: Any) -> str:
    return SEVERITY_LABELS_ZH.get(str(severity or "").lower(), str(severity or "中"))


def impact_zh(direction: Any) -> str:
    return IMPACT_LABELS_ZH.get(str(direction or "").lower(), str(direction or "中性"))


def horizon_zh(horizon: Any) -> str:
    return HORIZON_LABELS_ZH.get(str(horizon or "").lower(), str(horizon or "短期"))


def instrument_type_zh(kind: Any) -> str:
    return INSTRUMENT_TYPE_LABELS_ZH.get(str(kind or "").upper(), str(kind or "未知"))


# ── 货币符号映射（与个股日报保持一致）──────────────────────────
CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "GBX": "",
    "HKD": "HK$",
    "CNY": "￥",
    "CNH": "￥",
    "CAD": "CA$",
    "AUD": "A$",
    "JPY": "¥",
    "CHF": "CHF ",
    "SEK": "SEK ",
    "NOK": "NOK ",
    "DKK": "DKK ",
}
_CURRENCY_SUFFIXES = {"GBX": "p"}


def format_money(value: Any, currency: str = "USD", digits: int = 0) -> str:
    """带货币符号的金额格式化。未知货币使用代码本身作为前缀，避免误导性的 $。

    非有限值返回 N/A（修改计划第三轮 3）。
    """
    number = finite_float(value)
    if number is None:
        return "N/A"
    currency = str(currency or "USD").upper()
    symbol = CURRENCY_SYMBOLS.get(currency)
    text = f"{number:,.{digits}f}"
    if symbol is not None:
        return symbol + text + _CURRENCY_SUFFIXES.get(currency, "")
    return currency + " " + text


def format_ratio_as_pct(value: Any, digits: int = 2, with_sign: bool = False) -> str:
    """把 0~1 比例格式化为百分数。例如 0.0646 -> 6.46%。

    禁止对已经是百分数的输入使用本函数（修改计划第三轮 2.2）。
    """
    number = finite_float(value)
    if number is None:
        return "N/A"
    text = f"{number * 100.0:,.{digits}f}%"
    if with_sign and number > 0:
        return "+" + text
    return text


def format_pct_value(value: Any, digits: int = 2, with_sign: bool = False) -> str:
    """把已经是百分数的数值格式化（输入已乘 100）。例如 -1.3381 -> -1.34%。

    禁止对 0~1 比例使用本函数（修改计划第三轮 2.2）。
    """
    number = finite_float(value)
    if number is None:
        return "N/A"
    text = f"{number:,.{digits}f}%"
    if with_sign and number > 0:
        return "+" + text
    return text


def format_number(value: Any, digits: int = 2) -> str:
    """普通数字格式化；非有限值返回 N/A（修改计划第三轮 3）。"""
    number = finite_float(value)
    if number is None:
        return "N/A"
    return f"{number:,.{digits}f}"


# 兼容旧调用点：仍按绝对值猜测单位（禁止在新代码中使用）。
def format_pct(value: Any, digits: int = 2, with_sign: bool = False) -> str:
    """[已弃用] 按绝对值猜测单位。新代码必须改用 ``format_ratio_as_pct`` / ``format_pct_value``。"""
    number = finite_float(value)
    if number is None:
        return "N/A"
    if abs(number) <= 1.5:
        number = number * 100.0
    text = f"{number:,.{digits}f}%"
    if with_sign and number > 0:
        return "+" + text
    return text


def pct_color_class(value: Any) -> str:
    """根据数值正负返回涨跌颜色 class（项目既有风格：绿涨红跌）。

    仅看符号，不猜测单位、不做任何 ×100 放大。
    """
    number = finite_float(value)
    if number is None:
        return ""
    if number > 0:
        return "up"
    if number < 0:
        return "down"
    return ""
