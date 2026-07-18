from __future__ import annotations

import datetime as dt
import math
from typing import Any

import pandas as pd

try:
    from ticker_mapping import normalize_yfinance_ticker
except ImportError:  # pragma: no cover - defensive fallback
    def normalize_yfinance_ticker(value: str) -> str:
        return str(value or "").strip().upper()


TICKER_CURRENCY_SUFFIXES = {
    ".DE": "EUR",
    ".PA": "EUR",
    ".AS": "EUR",
    ".MI": "EUR",
    ".MC": "EUR",
    ".L": "GBX",
    ".HK": "HKD",
    ".SS": "CNY",
    ".SZ": "CNY",
    ".KS": "KRW",
    ".TO": "CAD",
    ".AX": "AUD",
    ".SW": "CHF",
    ".T": "JPY",
}


def normalize_currency(currency: Any) -> str:
    value = str(currency or "").strip().upper()
    if value in {"GBp", "GBX"}:
        return "GBX"
    return value


def infer_ticker_currency(ticker: str, fallback: str = "USD") -> str:
    ticker_upper = str(ticker or "").upper()
    if ticker_upper.endswith("=X"):
        return ""
    if ticker_upper.endswith("-USD"):
        return "USD"
    for suffix, currency in TICKER_CURRENCY_SUFFIXES.items():
        if ticker_upper.endswith(suffix):
            return currency
    return fallback


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _convert(value: float | None, from_currency: str, to_currency: str, fx_rates: dict[str, float], missing_fx: list[str]) -> float | None:
    if value is None:
        return None
    from_currency = normalize_currency(from_currency)
    to_currency = normalize_currency(to_currency)
    if not from_currency or not to_currency or from_currency == to_currency:
        return value
    if from_currency == "GBX":
        value = value / 100.0
        from_currency = "GBP"
    if to_currency == "GBX":
        converted = _convert(value, from_currency, "GBP", fx_rates, missing_fx)
        return converted * 100.0 if converted is not None else None
    key = f"{from_currency}{to_currency}"
    inverse = f"{to_currency}{from_currency}"
    rate = fx_rates.get(key)
    if rate:
        return value * float(rate)
    inverse_rate = fx_rates.get(inverse)
    if inverse_rate:
        return value / float(inverse_rate)
    if key not in missing_fx:
        missing_fx.append(key)
    return None


def build_portfolio_snapshot(
    portfolio_page: dict[str, Any],
    market_rows: list[dict[str, Any]] | None = None,
    *,
    latest_prices: dict[str, float] | None = None,
    fx_rates: dict[str, float] | None = None,
    base_currency: str = "EUR",
    benchmark: str = "^GSPC",
    as_of: dt.datetime | None = None,
    instrument_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic portfolio snapshot from holdings and market data.

    The returned dict is safe to serialize and contains no user identity or
    email address.  Absolute values are kept local for the HTML builder; callers
    should avoid forwarding them to an LLM unless the user explicitly opts in.
    """
    base_currency = normalize_currency(base_currency) or "EUR"
    fx_rates = dict(fx_rates or {})
    latest_prices = dict(latest_prices or {})
    market_by_ticker = {}
    for row in market_rows or []:
        ticker = normalize_yfinance_ticker(row.get("Ticker"))
        if ticker:
            market_by_ticker[ticker] = row

    holdings = []
    missing_prices: list[str] = []
    missing_fx: list[str] = []
    total_value = 0.0
    total_cost = 0.0

    for raw in portfolio_page.get("holdings", []) or []:
        ticker = normalize_yfinance_ticker(raw.get("ticker"))
        if not ticker:
            continue
        market = market_by_ticker.get(ticker, {})
        # Prefer caller-supplied market rows because the Streamlit UI already
        # has a coherent price snapshot.  Background schedules, which do not
        # have page state, fall back to freshly downloaded latest prices.
        price = _finite_float(market.get("Price"))
        if price is None:
            price = _finite_float(latest_prices.get(ticker))
        shares = _finite_float(raw.get("shares"))
        buy_price = _finite_float(raw.get("buy_price"))
        buy_currency = normalize_currency(raw.get("buy_currency"))
        ticker_currency = normalize_currency(market.get("Currency")) or infer_ticker_currency(ticker, buy_currency or "USD")

        market_value_native = price * shares if price is not None and shares is not None else None
        cost_basis_native = buy_price * shares if buy_price is not None and shares is not None else None
        market_value_base = _convert(market_value_native, ticker_currency, base_currency, fx_rates, missing_fx)
        cost_basis_base = _convert(cost_basis_native, buy_currency or ticker_currency, base_currency, fx_rates, missing_fx)
        profit_loss_base = (
            market_value_base - cost_basis_base
            if market_value_base is not None and cost_basis_base is not None
            else None
        )
        profit_loss_pct = (
            profit_loss_base / cost_basis_base * 100.0
            if profit_loss_base is not None and cost_basis_base not in (None, 0)
            else None
        )
        if market_value_base is not None and market_value_base > 0:
            total_value += market_value_base
        if cost_basis_base is not None and cost_basis_base > 0:
            total_cost += cost_basis_base
        if price is None:
            missing_prices.append(ticker)

        holdings.append({
            "ticker": ticker,
            # 修改计划 2.5 / 10.3：原始 group 视为账户分组，不再误当作行业。
            "group": str(raw.get("group") or "Portfolio"),
            "account_group": str(raw.get("group") or "Portfolio"),
            "shares": shares,
            "buy_price": buy_price,
            "buy_currency": buy_currency,
            "price": price,
            "price_currency": ticker_currency,
            "market_value_native": market_value_native,
            "market_value_base": market_value_base,
            "cost_basis_native": cost_basis_native,
            "cost_basis_base": cost_basis_base,
            "profit_loss_base": profit_loss_base,
            "profit_loss_pct": profit_loss_pct,
            "weight": 0.0,
            "name": market.get("Name") or ticker,
            "beta": _finite_float(market.get("Beta")),
            "rsi": _finite_float(market.get("RSI")),
            "volume_ratio": _finite_float(market.get("Volume_Ratio")),
            "return_1d": _finite_float(market.get("1D%")),
            "return_5d": _finite_float(market.get("5D%")),
            "return_1m": _finite_float(market.get("1M%")),
            "return_ytd": _finite_float(market.get("YTD%")),
            "diff_ema20": _finite_float(market.get("Diff_EMA20%")),
            "diff_ema50": _finite_float(market.get("Diff_EMA50%")),
            "diff_ema200": _finite_float(market.get("Diff_EMA200%")),
        })

        # 附加工具类型元数据（account_group 之外区分 sector/industry/theme/asset_class）
        if instrument_metadata:
            m = instrument_metadata.get(ticker)
            if m:
                holdings[-1].update({
                    "instrument_type": m.get("instrument_type"),
                    "asset_class": m.get("asset_class"),
                    "sector": m.get("sector"),
                    "industry": m.get("industry"),
                    "theme": m.get("theme"),
                    "underlying_index": m.get("underlying_index"),
                    "exchange": m.get("exchange"),
                })

    for holding in holdings:
        value = holding.get("market_value_base")
        holding["weight"] = value / total_value if value is not None and total_value > 0 else 0.0

    return {
        "portfolio_id": str(portfolio_page.get("id") or ""),
        "portfolio_name": str(portfolio_page.get("name") or "Portfolio"),
        "as_of": (as_of or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"),
        "base_currency": base_currency,
        "benchmark": normalize_yfinance_ticker(benchmark) or "^GSPC",
        "holdings": holdings,
        "summary": {
            "total_market_value_base": total_value if total_value > 0 else None,
            "total_cost_basis_base": total_cost if total_cost > 0 else None,
            "profit_loss_base": (total_value - total_cost) if total_value > 0 and total_cost > 0 else None,
            "profit_loss_pct": ((total_value - total_cost) / total_cost * 100.0) if total_cost > 0 else None,
        },
        "data_quality": {
            "missing_prices": sorted(set(missing_prices)),
            "missing_fx": sorted(set(missing_fx)),
            "missing_history": [],
        },
        "instrument_metadata": instrument_metadata or {},
    }


def close_from_yfinance_download(data: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Extract an adjusted-close frame from a yfinance style DataFrame."""
    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        field_names = list(data.columns.get_level_values(0))
        field = "Adj Close" if "Adj Close" in field_names else "Close"
        close = data.xs(field, axis=1, level=0)
    else:
        field = "Adj Close" if "Adj Close" in data.columns else "Close"
        close = data[[field]].rename(columns={field: tickers[0] if tickers else "Ticker"})
    close = close.copy()
    close.columns = [normalize_yfinance_ticker(c) for c in close.columns]
    ordered = [ticker for ticker in tickers if ticker in close.columns]
    return close[ordered].dropna(axis=1, how="all") if ordered else pd.DataFrame()
