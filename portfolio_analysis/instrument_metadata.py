# -*- coding: utf-8 -*-
"""工具类型识别与元数据层。

解决问题：
- 搜索词不识别股票 / ETF / ETC / 指数 / 加密资产（修改计划 2.4）；
- Portfolio 账户分组被错误当作行业（修改计划 2.5、10.3）；
- 同一资产的欧洲挂牌版本需要同时用 ticker + 全称 + 底层指数识别（修改计划 11.6）。

设计原则：
- 默认纯本地启发式，不发起网络请求，保证确定性测试；
- 可选 ``enrich`` 调用 yfinance ``.info`` 作为补充（项目市场数据优先级最高）；
- 输出区分 ``account_group``（券商/账户）与 ``sector`` / ``industry`` / ``theme`` / ``asset_class`` / ``underlying_index``。
"""
from __future__ import annotations

import re
from typing import Any

try:
    from ticker_mapping import normalize_yfinance_ticker
except ImportError:  # pragma: no cover - 防御性兜底
    def normalize_yfinance_ticker(value: str) -> str:
        return str(value or "").strip().upper()


INSTRUMENT_TYPES = {
    "EQUITY", "ETF", "ETC", "INDEX", "CRYPTO", "FUND", "COMMODITY", "UNKNOWN",
}

_EXCHANGE_BY_SUFFIX = {
    ".DE": "XETRA", ".PA": "EURONEXT", ".AS": "EURONEXT", ".MI": "Borsa Italiana",
    ".MC": "BME", ".L": "LSE", ".HK": "HKEX", ".SS": "SSE", ".SZ": "SZSE",
    ".KS": "KRX", ".TO": "TSX", ".AX": "ASX", ".SW": "SIX", ".T": "TSE",
}

_ETF_HINTS = [
    "etf", "ucits", "ishares", "vanguard", "invesco", "xtrackers", "spdr",
    "lyxor", "amundi", "vanECK", "vaneck", "wisdomtree", "db x-trackers",
    "comstage", "justetf", "xetra", "franklin", "bnp", "hsbc", "state street",
    "charles schwab", "schwab", "proshares", "global x", "first trust",
]
_ETC_HINTS = [
    "etc", "physical gold", "physical silver", "gold etc", "silver etc",
    "xetra-gold", "commodity", "uranium", "platinum etc", "palladium etc",
]
_INDEX_HINTS = [
    "index", "nasdaq-100", "nasdaq 100", "s&p 500", "s&p500", "msci",
    "ftse", "euro stoxx", "dax", "stoxx", "russell", "cac 40", "ibex",
]
_FUND_HINTS = ["mutual fund", "fund of funds", "index fund", "active fund"]

# 已知底层指数 / 主题映射（用于 ETF / 指数基金）
_UNDERLYING_INDEX_PATTERNS = [
    ("Nasdaq-100", ["nasdaq-100", "nasdaq 100", "ndx"]),
    ("S&P 500", ["s&p 500", "s&p500", "sp500", "sp 500"]),
    ("MSCI World", ["msci world", "msci acwi", "msci all country"]),
    ("MSCI Emerging Markets", ["msci em", "msci emerging"]),
    ("S&P 500 Information Technology", ["information technology", "tech sector"]),
    ("Euro Stoxx 50", ["euro stoxx 50", "eurostoxx 50"]),
    ("DAX", ["^dax", " dax"]),
    ("FTSE 100", ["ftse 100"]),
    ("Gold", ["gold", "physical gold"]),
    ("Bitcoin", ["bitcoin", "btc"]),
]

_CRYPTO_SUFFIX = "-USD"
_KNOWN_CRYPTO = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "DOT", "AVAX", "MATIC", "LINK"}


def _norm(text: str) -> str:
    return (text or "").lower()


def _exchange_for(ticker: str) -> str:
    for suffix, exchange in _EXCHANGE_BY_SUFFIX.items():
        if ticker.upper().endswith(suffix):
            return exchange
    if ticker.upper().endswith(_CRYPTO_SUFFIX):
        return "CRYPTO"
    return "UNKNOWN"


def _infer_underlying_index(name: str) -> str | None:
    low = _norm(name)
    for index, patterns in _UNDERLYING_INDEX_PATTERNS:
        if any(p in low for p in patterns):
            return index
    return None


def _infer_theme(instrument_type: str, name: str, underlying_index: str | None) -> str | None:
    low = _norm(name)
    if underlying_index:
        if "nasdaq-100" in underlying_index.lower():
            return "Nasdaq-100 / 美国大盘成长"
        if "s&p 500" in underlying_index.lower():
            return "S&P 500 / 美国全市场"
        if "msci world" in underlying_index.lower():
            return "MSCI World / 全球发达市场"
        if "gold" in underlying_index.lower() or "gold" in low:
            return "黄金 / 避险商品"
        if "bitcoin" in underlying_index.lower():
            return "比特币 / 加密资产"
        return underlying_index
    if instrument_type == "CRYPTO":
        return "加密资产"
    if "semiconductor" in low or "chip" in low:
        return "半导体"
    if "technology" in low or "tech" in low:
        return "科技"
    return None


def classify_instrument(
    ticker: str,
    name: str | None = None,
    quote_type: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """返回一个持仓的工具元数据字典（不含 account_group）。"""
    ticker_u = normalize_yfinance_ticker(ticker)
    low_name = _norm(name)
    low_qt = _norm(quote_type)

    # 1) 加密资产：ticker 后缀或已知符号
    if ticker_u.endswith(_CRYPTO_SUFFIX) or ticker_u.split("-")[0] in _KNOWN_CRYPTO and ticker_u.endswith("USD"):
        instrument_type = "CRYPTO"
        asset_class = "Crypto"
        underlying_index = _infer_underlying_index(name or ticker_u)
    elif low_qt == "crypto" or (ticker_u.endswith(_CRYPTO_SUFFIX) and low_qt in {"", "equity"}):
        instrument_type = "CRYPTO"
        asset_class = "Crypto"
        underlying_index = _infer_underlying_index(name or ticker_u)
    else:
        # 2) 个股（quote_type 明确为 equity 时优先）
        if low_qt == "equity":
            instrument_type = "EQUITY"
            asset_class = "Equity"
            underlying_index = None
        # 3) ETC / 商品（优先于 ETF：如 Xetra-Gold 同时含 xetra / gold）
        elif any(h in low_name for h in _ETC_HINTS):
            instrument_type = "ETC"
            asset_class = "Commodity ETC"
            underlying_index = _infer_underlying_index(name or ticker_u)
        # 4) ETF / 基金（ishares / vanguard / ucits / msci world etf ...）
        elif low_qt in {"etf", "mutualfund"} or any(h in low_name for h in _ETF_HINTS) or any(h in low_name for h in _FUND_HINTS):
            instrument_type = "ETF" if low_qt != "mutualfund" else "FUND"
            asset_class = "Equity ETF" if not any(x in low_name for x in ["bond", "treasury", "fixed income", "aggregate"]) else "Bond ETF"
            underlying_index = _infer_underlying_index(name or ticker_u)
        # 5) 指数（必须在 ETF 之后，避免把『跟踪某指数的 ETF』误判为指数本身）
        elif low_qt == "index" or any(h in low_name for h in _INDEX_HINTS):
            instrument_type = "INDEX"
            asset_class = "Index"
            underlying_index = _infer_underlying_index(name or ticker_u) or (name or ticker_u)
        else:
            # 未命中任何特殊类型时，组合持仓绝大多数是个股，默认视作 EQUITY，
            # 以保证后续的 instrument-aware 新闻查询与重复暴露检测能正常工作。
            instrument_type = "EQUITY"
            asset_class = "Equity"
            underlying_index = None

    theme = _infer_theme(instrument_type, name or ticker_u, underlying_index)
    return {
        "ticker": ticker_u,
        "name": name or ticker_u,
        "instrument_type": instrument_type,
        "asset_class": asset_class,
        "sector": None,
        "industry": None,
        "theme": theme,
        "underlying_index": underlying_index,
        "exchange": _exchange_for(ticker_u),
        "currency": None,
        "account_group": None,
    }


def build_instrument_metadata(
    portfolio_page: dict[str, Any],
    *,
    enrich: bool = False,
) -> dict[str, dict[str, Any]]:
    """为 portfolio_page 的持仓构建 ticker -> 元数据 映射。

    返回的元数据已包含 ``account_group``（来自原始 group 字段）。
    """
    meta: dict[str, dict[str, Any]] = {}
    for raw in portfolio_page.get("holdings", []) or []:
        ticker = normalize_yfinance_ticker(raw.get("ticker"))
        if not ticker:
            continue
        group = str(raw.get("group") or "Portfolio")
        m = classify_instrument(ticker, name=raw.get("name"))
        m["account_group"] = group
        if enrich:
            m = _enrich_via_yfinance(ticker, m)
        meta[ticker] = m
    return meta


def _enrich_via_yfinance(ticker: str, meta: dict[str, Any]) -> dict[str, Any]:
    """可选：用 yfinance ``.info`` 补充 quoteType / longName / category。"""
    try:
        import yfinance as yf  # 延迟导入，避免无网络环境强制依赖
        info = yf.Ticker(ticker).info or {}
        qt = _norm(info.get("quoteType"))
        if qt == "etf":
            meta["instrument_type"] = "ETF"
        elif qt == "index":
            meta["instrument_type"] = "INDEX"
        elif qt == "crypto":
            meta["instrument_type"] = "CRYPTO"
        elif qt == "equity":
            meta["instrument_type"] = "EQUITY"
        long_name = info.get("longName") or info.get("shortName")
        if long_name and not meta.get("name"):
            meta["name"] = long_name
        category = info.get("category") or info.get("fundCategory")
        if category:
            meta["asset_class"] = category
        if info.get("currency"):
            meta["currency"] = str(info["currency"]).upper()
    except Exception:
        pass
    return meta


def is_equity_like(meta: dict[str, Any]) -> bool:
    return str(meta.get("instrument_type", "")).upper() in {"EQUITY"}


def is_searchable_as_stock(meta: dict[str, Any]) -> bool:
    """是否应使用「公司财报/分析师」式新闻查询。"""
    return str(meta.get("instrument_type", "")).upper() in {"EQUITY"}
