# -*- coding: utf-8 -*-
"""工具类型识别与元数据层。

解决问题（修改计划第三轮 9）：
- 真实持仓往往没有 name，必须用 market rows / 缓存 quote_type；
- BTC-EUR 等 ``BASE-FIAT`` 对要识别为 CRYPTO；
- long name 必须能覆盖占位 ticker 名；
- 修改工具类型后必须重算 asset_class / theme / underlying_index / sector / industry；
- uranium 是主题不是产品法律结构（ETF 仍归 ETF，主题标 Uranium & Nuclear）；
- 绝不要把账户分组当行业。

设计原则：
- 默认纯本地启发式，不发起网络请求，保证确定性测试；
- 可选 enrich 调用 yfinance ``.info`` 作为补充（优先级低于 market rows / 缓存）；
- 输出区分 ``account_group``（券商/账户）与 ``sector`` / ``industry`` / ``theme`` /
  ``asset_class`` / ``underlying_index``。
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

_KNOWN_CRYPTO = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "DOT", "AVAX", "MATIC", "LINK", "LTC", "TRX", "ATOM", "UNI"}
_KNOWN_FIAT = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD"}

_EXCHANGE_BY_SUFFIX = {
    ".DE": "XETRA", ".PA": "EURONEXT", ".AS": "EURONEXT", ".MI": "Borsa Italiana",
    ".MC": "BME", ".L": "LSE", ".HK": "HKEX", ".SS": "SSE", ".SZ": "SZSE",
    ".KS": "KRX", ".TO": "TSX", ".AX": "ASX", ".SW": "SIX", ".T": "TSE",
}

# 注意：uranium 已从 ETC 提示词移除——它是主题而非产品法律结构（修改计划第三轮 9）。
_ETF_HINTS = [
    "etf", "ucits", "ishares", "vanguard", "invesco", "xtrackers", "spdr",
    "lyxor", "amundi", "vanECK", "vaneck", "wisdomtree", "db x-trackers",
    "comstage", "justetf", "xetra", "franklin", "bnp", "hsbc", "state street",
    "charles schwab", "schwab", "proshares", "global x", "first trust",
]
_ETC_HINTS = [
    "etc", "physical gold", "physical silver", "gold etc", "silver etc",
    "xetra-gold", "commodity etc", "platinum etc", "palladium etc",
    "physical palladium", "physical platinum",
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

INSTRUMENT_OVERRIDES: dict[str, dict[str, Any]] = {
    # Research identity overrides.  These fields are intentionally local and
    # deterministic: they let the Official Lane and Entity Resolver recognise
    # the issuer/company without requiring a live yfinance ``info`` call.
    "SOFI": {
        "name": "SoFi Technologies, Inc.",
        "entity_aliases": ["SoFi", "SoFi Technologies"],
        "localized_aliases": ["SoFi科技", "SoFi金融"],
        "official_domains": ["sofi.com", "investors.sofi.com"],
        "ir_domain": "investors.sofi.com",
    },
    "TSLA": {
        "name": "Tesla, Inc.",
        "entity_aliases": ["Tesla", "Tesla Motors"],
        "localized_aliases": ["特斯拉"],
        "official_domains": ["tesla.com", "ir.tesla.com"],
        "ir_domain": "ir.tesla.com",
    },
    "META": {
        "name": "Meta Platforms, Inc.",
        "entity_aliases": ["Meta Platforms", "Meta", "Facebook"],
        "localized_aliases": ["脸书", "Meta平台"],
        "official_domains": ["investor.atmeta.com", "atmeta.com", "about.fb.com"],
        "ir_domain": "investor.atmeta.com",
    },
    "VST": {
        "name": "Vistra Corp.",
        "entity_aliases": ["Vistra", "Vistra Corp"],
        "localized_aliases": ["维斯特拉"],
        "official_domains": ["vistracorp.com", "investor.vistracorp.com"],
        "ir_domain": "investor.vistracorp.com",
    },
    "ORCL": {
        "name": "Oracle Corporation",
        "entity_aliases": ["Oracle", "Oracle Corporation"],
        "localized_aliases": ["甲骨文"],
        "official_domains": ["oracle.com", "investor.oracle.com"],
        "ir_domain": "investor.oracle.com",
    },
    "MSFT": {
        "name": "Microsoft Corporation",
        "localized_aliases": ["微软"],
        "entity_aliases": ["Microsoft", "Microsoft Corporation"],
        "official_domains": ["microsoft.com"],
        "ir_domain": "microsoft.com/en-us/investor",
    },
    "NVDA": {
        "name": "NVIDIA Corporation",
        "entity_aliases": ["NVIDIA", "Nvidia Corporation"],
        "localized_aliases": ["英伟达", "辉达"],
        "official_domains": ["nvidia.com", "investor.nvidia.com"],
        "ir_domain": "investor.nvidia.com",
    },
    "NVD.DE": {
        "name": "NVIDIA Corporation",
        "entity_aliases": ["NVIDIA", "Nvidia Corporation", "NVDA"],
        "localized_aliases": ["英伟达", "辉达"],
        "official_domains": ["nvidia.com", "investor.nvidia.com"],
        "ir_domain": "investor.nvidia.com",
    },
    "UNH": {
        "name": "UnitedHealth Group Incorporated",
        "entity_aliases": ["UnitedHealth Group", "UnitedHealth", "UnitedHealthcare"],
        "localized_aliases": ["联合健康", "联合健康集团"],
        "official_domains": ["unitedhealthgroup.com"],
        "ir_domain": "unitedhealthgroup.com/investors",
    },
    "JPM": {
        "name": "JPMorgan Chase & Co.",
        "entity_aliases": ["JPMorgan Chase", "JPMorgan", "J.P. Morgan"],
        "localized_aliases": ["摩根大通"],
        "official_domains": ["jpmorganchase.com"],
        "ir_domain": "jpmorganchase.com/ir",
    },
    "COIN": {
        "name": "Coinbase Global, Inc.",
        "entity_aliases": ["Coinbase", "Coinbase Global"],
        "localized_aliases": ["Coinbase交易所"],
        "official_domains": ["coinbase.com", "investor.coinbase.com"],
        "ir_domain": "investor.coinbase.com",
    },
    "WNUC.DE": {
        "name": "WisdomTree Uranium and Nuclear Energy UCITS ETF",
        "entity_aliases": [
            "WisdomTree Uranium and Nuclear Energy UCITS ETF",
            "WisdomTree Uranium and Nuclear Energy",
            "WNUC",
        ],
        "official_domains": ["wisdomtree.eu", "wisdomtree.com"],
        "issuer_domain": "wisdomtree.eu",
        "theme": "Uranium & Nuclear Energy",
        "key_drivers": ["uranium", "nuclear energy", "uranium supply", "nuclear policy"],
    },
    "LYMS.DE": {
        "name": "Amundi Core Nasdaq-100 Swap UCITS ETF Acc",
        "entity_aliases": [
            "Amundi Core Nasdaq-100 Swap UCITS ETF",
            "Amundi Nasdaq-100 UCITS ETF",
            "LYMS",
        ],
        "official_domains": ["amundietf.com", "amundi.com"],
        "issuer_domain": "amundietf.com",
        "underlying_index": "Nasdaq-100",
        "theme": "Nasdaq-100 / 美国大盘成长",
        "key_drivers": ["Nasdaq-100", "large cap technology", "US growth equities"],
    },
    "PPFB.DE": {
        "instrument_type": "ETC",
        "asset_class": "Precious Metal ETC",
        "theme": "Gold / Precious Metals",
        "classification_source": "manual",
        "classification_confidence": 1.0,
        "needs_review": False,
        "entity_aliases": ["iShares Physical Gold ETC", "iShares Physical Metals"],
        "official_domains": ["ishares.com", "blackrock.com"],
        "issuer_domain": "ishares.com",
        "key_drivers": ["gold price", "real yields", "central bank gold demand"],
    },
}


def _norm(text: str) -> str:
    return (text or "").lower()


def _exchange_for(ticker: str) -> str:
    for suffix, exchange in _EXCHANGE_BY_SUFFIX.items():
        if ticker.upper().endswith(suffix):
            return exchange
    parts = ticker.upper().split("-")
    if len(parts) == 2 and parts[0] in _KNOWN_CRYPTO:
        return "CRYPTO"
    if ticker.upper().endswith("-USD"):
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
    if "uranium" in low or "nuclear" in low:
        return "Uranium & Nuclear / 核能与铀"
    if "semiconductor" in low or "chip" in low:
        return "半导体"
    if "technology" in low or "tech" in low:
        return "科技"
    if "gold" in low or "silver" in low or "precious metal" in low:
        return "黄金 / 避险商品"
    return None


def classify_instrument(
    ticker: str,
    name: str | None = None,
    quote_type: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """返回一个持仓的工具元数据字典（不含 account_group）。

    优先级（修改计划第三轮 9.3）：crypto 对 > equity > ETC > ETF/FUND > INDEX > 默认 EQUITY。
    """
    ticker_u = normalize_yfinance_ticker(ticker)
    low_name = _norm(name)
    low_qt = _norm(quote_type)

    # 1) 加密资产对：BASE-FIAT 或 BASE-USD（修改计划第三轮 9.3）
    parts = ticker_u.split("-")
    if len(parts) == 2 and parts[0] in _KNOWN_CRYPTO and parts[1] in _KNOWN_FIAT:
        instrument_type = "CRYPTO"
        asset_class = "Crypto"
        underlying_index = _infer_underlying_index(name or ticker_u)
    elif ticker_u.endswith("-USD") or (low_qt == "crypto"):
        instrument_type = "CRYPTO"
        asset_class = "Crypto"
        underlying_index = _infer_underlying_index(name or ticker_u)
    elif low_qt == "equity":
        instrument_type = "EQUITY"
        asset_class = "Equity"
        underlying_index = None
    # ETC / 商品（优先于 ETF：如 Xetra-Gold 同时含 xetra / gold）
    elif any(h in low_name for h in _ETC_HINTS):
        instrument_type = "ETC"
        asset_class = "Commodity ETC"
        underlying_index = _infer_underlying_index(name or ticker_u)
    # ETF / 基金
    elif low_qt in {"etf", "mutualfund"} or any(h in low_name for h in _ETF_HINTS) or any(h in low_name for h in _FUND_HINTS):
        instrument_type = "ETF" if low_qt != "mutualfund" else "FUND"
        asset_class = "Equity ETF" if not any(x in low_name for x in ["bond", "treasury", "fixed income", "aggregate"]) else "Bond ETF"
        underlying_index = _infer_underlying_index(name or ticker_u)
    # 指数（必须在 ETF 之后，避免把『跟踪某指数的 ETF』误判为指数本身）
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
        "classification_source": "heuristic" if instrument_type != "EQUITY" else "default",
        "classification_confidence": 0.75 if instrument_type != "EQUITY" else 0.4,
        "needs_review": instrument_type == "EQUITY" and not low_qt,
        # 第六轮（修改计划第 14.1 节）：官方来源字段，供 Planner 和 source_lanes 使用
        "official_domains": [],
        "ir_domain": None,
        "sec_cik": None,
        "issuer_domain": None,
        "regulator_domains": [],
        "key_drivers": [],
        "known_upcoming_events": [],
    }


def _finalize_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """在 instrument_type 确定（或经 enrich 修改）后，重算主题 / 底层指数 / 资产类 / 行业。"""
    instrument_type = str(meta.get("instrument_type") or "UNKNOWN").upper()
    name = meta.get("name") or meta.get("ticker") or ""
    if instrument_type == "EQUITY":
        meta["asset_class"] = "Equity"
        meta["underlying_index"] = None
    elif instrument_type == "ETF":
        meta["asset_class"] = "Equity ETF" if "bond" not in _norm(name) else "Bond ETF"
    elif instrument_type == "ETC":
        meta["asset_class"] = "Commodity ETC"
    elif instrument_type == "CRYPTO":
        meta["asset_class"] = "Crypto"
    elif instrument_type == "INDEX":
        meta["asset_class"] = "Index"
    elif instrument_type == "FUND":
        meta["asset_class"] = meta.get("asset_class") or "Fund"
    meta["underlying_index"] = _infer_underlying_index(name)
    meta["theme"] = _infer_theme(instrument_type, name, meta.get("underlying_index"))
    return meta


def _name_from_market_row(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    for key in ("Name", "name", "LongName", "longName"):
        v = row.get(key)
        if v and str(v).strip():
            return str(v).strip()
    return None


def build_instrument_metadata(
    portfolio_page: dict[str, Any],
    *,
    market_rows: list[dict[str, Any]] | None = None,
    cached_ticker_info: dict[str, dict[str, Any]] | None = None,
    enrich: bool = False,
) -> dict[str, dict[str, Any]]:
    """为 portfolio_page 的持仓构建 ticker -> 元数据 映射（修改计划第三轮 9）。

    名称来源优先级：
        1) Streamlit/Backend 已有 market rows；
        2) 项目 ticker info 缓存（cached_ticker_info）；
        3) yfinance info/fast_info（enrich=True）；
        4) ticker/name 启发式；
        5) UNKNOWN。
    """
    meta: dict[str, dict[str, Any]] = {}
    rows_by_ticker = {
        normalize_yfinance_ticker(r.get("Ticker")): r
        for r in (market_rows or []) if r.get("Ticker")
    }
    cached_ticker_info = cached_ticker_info or {}

    for raw in portfolio_page.get("holdings", []) or []:
        ticker = normalize_yfinance_ticker(raw.get("ticker"))
        if not ticker:
            continue
        group = str(raw.get("group") or "Portfolio")
        row = rows_by_ticker.get(ticker)
        name = (
            (raw.get("name") or "").strip()
            or _name_from_market_row(row)
            or ticker
        )

        # 优先级 2：缓存 quote type / longName / category
        cached = cached_ticker_info.get(ticker) or {}
        quote_type = cached.get("quoteType") or cached.get("quote_type")
        category = cached.get("category") or cached.get("fundCategory")

        m = classify_instrument(ticker, name=name, quote_type=quote_type, category=category)
        m["account_group"] = group
        if row and _name_from_market_row(row):
            m["classification_source"] = "market_row"
            m["classification_confidence"] = max(float(m.get("classification_confidence") or 0.0), 0.8)
            m["needs_review"] = False
        if quote_type:
            m["classification_source"] = "cache"
            m["classification_confidence"] = 0.95
            m["needs_review"] = False

        # 应用缓存 longName
        cached_long = cached.get("longName") or cached.get("shortName")
        if cached_long and (not m.get("name") or m["name"] == ticker):
            m["name"] = cached_long

        # 优先级 3：yfinance enrich（低于 market rows / 缓存）
        if enrich:
            m = _enrich_via_yfinance(ticker, m)

        # long name 覆盖占位 ticker 名（修改计划第三轮 9.3）
        market_long = _name_from_market_row(row)
        if market_long and (not m.get("name") or m["name"] == ticker):
            m["name"] = market_long

        m = _finalize_metadata(m)
        override = INSTRUMENT_OVERRIDES.get(ticker)
        if override:
            # Preserve a concrete name supplied by the portfolio/market cache.
            # Research overrides enrich identity metadata; they must not silently
            # rewrite a user-visible product name when the same exchange ticker is
            # reused for a different share class/product in a test or deployment.
            override_fields = dict(override)
            override_name = override_fields.pop("name", None)
            current_name = str(m.get("name") or "").strip()
            m.update(override_fields)
            if override_name and (not current_name or current_name.upper() == ticker.upper()):
                m["name"] = override_name
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
        if long_name and (not meta.get("name") or meta["name"] == ticker):
            meta["name"] = long_name
        category = info.get("category") or info.get("fundCategory")
        if category:
            meta["asset_class"] = category
        if info.get("currency"):
            meta["currency"] = str(info["currency"]).upper()
        if qt:
            meta["classification_source"] = "yfinance"
            meta["classification_confidence"] = 0.95
            meta["needs_review"] = False
    except Exception:
        pass
    return meta


def is_equity_like(meta: dict[str, Any]) -> bool:
    return str(meta.get("instrument_type", "")).upper() in {"EQUITY"}


def is_searchable_as_stock(meta: dict[str, Any]) -> bool:
    """是否应使用「公司财报/分析师」式新闻查询。"""
    return str(meta.get("instrument_type", "")).upper() in {"EQUITY"}
