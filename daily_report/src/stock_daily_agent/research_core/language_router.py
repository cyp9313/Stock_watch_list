# -*- coding: utf-8 -*-
"""Search Language Router（修改计划第六轮第 6 节）。

核心规则（修改计划 2.4）：
- A 股（.SS / .SZ）：中文为主；
- 非 A 股（美股 / 欧股 / 港股 / ETF / ETC / 指数 / Crypto）：英文默认；
- 本地语言补搜仅在「英文第一轮不足 / 官方 IR 使用本地语言 / 监管机构仅发布
  本地语言」时触发。

注意事项：
- 不能把港股 .HK 当 A 股；
- 不能把中概股美股当 A 股；
- 不能因为公司来自中国就自动使用中文。
"""
from __future__ import annotations

from typing import Any


class LanguageRoutingError(RuntimeError):
    """语言路由不可恢复错误。"""


# A 股后缀（上交所 / 深交所）
_A_SHARE_SUFFIXES = (".SS", ".SZ")

# 港股后缀（明确不是 A 股）
_HK_SUFFIX = ".HK"


def is_a_share(ticker: str) -> bool:
    """判断一个 ticker 是否为 A 股（.SS / .SZ）。

    注意：港股 ``.HK``、中概股美股、A 股公司海外 ADR 都不算 A 股。
    """
    if not ticker:
        return False
    t = str(ticker).strip().upper()
    return t.endswith(_A_SHARE_SUFFIXES)


def _market_for(ticker: str, meta: dict[str, Any]) -> str:
    """根据 ticker 后缀和元数据推断市场标签。"""
    t = str(ticker or "").strip().upper()
    if t.endswith(_A_SHARE_SUFFIXES):
        return "CN"
    if t.endswith(_HK_SUFFIX):
        return "HK"
    if t.endswith(".DE"):
        return "DE"
    if t.endswith(".PA") or t.endswith(".AS") or t.endswith(".MI") or t.endswith(".MC"):
        return "EU"
    if t.endswith(".L"):
        return "GB"
    if t.endswith(".T") or t.endswith(".KS"):
        return "ASIA"
    if t.endswith(".TO") or t.endswith(".AX"):
        return "OTHER"
    if "-" in t and t.split("-")[0] in {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE"}:
        return "CRYPTO"
    market = str(meta.get("market") or "").upper()
    if market:
        return market
    # 默认按美股处理（包括中概股 ADR）
    return "US"


def determine_search_language(
    ticker: str,
    instrument_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回单个 ticker 的搜索语言策略。

    返回结构：
        {
            "primary_language": "en" | "zh-CN",
            "fallback_languages": ["zh-CN" | "de" | "fr" | ...],
            "reason": "non_a_share_default_english"
                      | "a_share_default_chinese"
                      | "explicit_override",
            "market": "CN" | "US" | "HK" | "DE" | "EU" | ...,
        }
    """
    instrument_metadata = instrument_metadata or {}
    meta = instrument_metadata.get(ticker, {}) if isinstance(instrument_metadata, dict) else {}
    if not isinstance(meta, dict):
        meta = {}

    # 显式覆盖（来自 instrument_metadata 的 search_language 字段）
    explicit = str(meta.get("search_language") or "").strip().lower()
    if explicit in {"en", "zh-cn", "zh", "de", "fr", "ja", "ko"}:
        normalized = "zh-CN" if explicit in {"zh", "zh-cn"} else explicit
        return {
            "primary_language": normalized,
            "fallback_languages": [],
            "reason": "explicit_override",
            "market": _market_for(ticker, meta),
        }

    market = _market_for(ticker, meta)
    if is_a_share(ticker):
        # A 股：中文为主；可选英文补搜仅当英文名可靠且国际媒体覆盖明显
        fallback: list[str] = []
        name = str(meta.get("name") or "").strip()
        # 简单启发式：名称包含拉丁字符时允许英文补搜
        if name and any(ch.isalpha() and ord(ch) < 128 for ch in name):
            fallback.append("en")
        return {
            "primary_language": "zh-CN",
            "fallback_languages": fallback,
            "reason": "a_share_default_chinese",
            "market": market,
        }

    # 非 A 股：英文默认
    fallback_languages: list[str] = []
    # 德国 / 法国 / 意大利 / 日本 / 韩国：本地语言仅作补搜
    if market == "DE":
        fallback_languages = ["de"]
    elif market == "EU":
        # 欧元区：法/荷/意/西
        if ticker.endswith(".PA"):
            fallback_languages = ["fr"]
        elif ticker.endswith(".MI"):
            fallback_languages = ["it"]
        elif ticker.endswith(".MC"):
            fallback_languages = ["es"]
        elif ticker.endswith(".AS"):
            fallback_languages = ["nl"]
    elif market == "ASIA":
        if ticker.endswith(".T"):
            fallback_languages = ["ja"]
        elif ticker.endswith(".KS"):
            fallback_languages = ["ko"]
    return {
        "primary_language": "en",
        "fallback_languages": fallback_languages,
        "reason": "non_a_share_default_english",
        "market": market,
    }
