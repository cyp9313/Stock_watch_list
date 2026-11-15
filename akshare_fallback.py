"""Typed mainland daily-data fallback for yfinance gaps.

``FUND:xxxxxx`` explicitly denotes an open-end fund's *unit NAV*, not OHLC.
Ordinary mainland symbols retain their yfinance spelling and are only routed
here after yfinance has returned no usable daily prices.
"""

from __future__ import annotations

from datetime import date, timedelta
import re

import pandas as pd


FUND_PATTERN = re.compile(r"^FUND:(\d{6})$", re.IGNORECASE)
MAINLAND_PATTERN = re.compile(r"^(\d{6})(?:\.(SS|SZ|BJ))?$", re.IGNORECASE)


def is_nav_symbol(ticker: str) -> bool:
    return bool(FUND_PATTERN.fullmatch(str(ticker).strip()))


def mainland_code(ticker: str) -> str | None:
    match = MAINLAND_PATTERN.fullmatch(str(ticker).strip())
    return match.group(1) if match else None


def eligible_for_fallback(ticker: str) -> bool:
    return is_nav_symbol(ticker) or mainland_code(ticker) is not None


def _start_date(period: str, end: date) -> date:
    match = re.fullmatch(r"(\d+)(d|wk|mo|y)", period)
    if not match:
        return end - timedelta(days=760)
    amount, unit = int(match.group(1)), match.group(2)
    days = amount * {"d": 1, "wk": 7, "mo": 31, "y": 366}[unit]
    return end - timedelta(days=days + 5)


def _normalize_ohlcv(raw: pd.DataFrame, *, source: str) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    renamed = raw.rename(columns={
        "日期": "Date", "开盘": "Open", "最高": "High", "最低": "Low",
        "收盘": "Close", "成交量": "Volume", "date": "Date", "open": "Open",
        "high": "High", "low": "Low", "close": "Close", "volume": "Volume",
    }).copy()
    required = ("Date", "Open", "High", "Low", "Close", "Volume")
    if any(col not in renamed for col in required):
        raise ValueError(f"{source} did not provide complete OHLCV columns")
    dates = pd.to_datetime(renamed["Date"], errors="coerce")
    result = renamed.loc[:, list(required[1:])].copy()
    result.index = pd.DatetimeIndex(dates, name="Date")
    for col in required[1:]:
        result[col] = pd.to_numeric(result[col], errors="coerce")
    result = result.loc[result.index.notna()].dropna(subset=["Open", "High", "Low", "Close"])
    result = result.loc[(result["Close"] > 0) & (result["High"] >= result["Low"])]
    # qfq is already adjusted; Sina provides raw prices, which are explicitly
    # tagged so reports never claim those returns include distributions.
    result["Adj Close"] = result["Close"]
    result = result.loc[:, ["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    result = result[~result.index.duplicated(keep="last")].sort_index()
    result.attrs.update(provider="akshare", price_basis="raw" if source == "ETF_SINA" else "qfq",
                        instrument_type="ETF" if source.startswith("ETF") else "EQUITY")
    return result


def _normalize_nav(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if "净值日期" not in raw or "单位净值" not in raw:
        raise ValueError("AkShare fund response lacks date/unit NAV")
    result = pd.DataFrame({"Close": pd.to_numeric(raw["单位净值"], errors="coerce")})
    result.index = pd.DatetimeIndex(pd.to_datetime(raw["净值日期"], errors="coerce"), name="Date")
    result = result.loc[result.index.notna()].dropna(subset=["Close"])
    result = result.loc[result["Close"] > 0]
    result["Adj Close"] = result["Close"]
    result = result[~result.index.duplicated(keep="last")].sort_index()
    result.attrs.update(provider="akshare", price_basis="unit_nav", instrument_type="MUTUALFUND")
    return result


def fetch_daily(ticker: str, *, period: str = "2y", start=None, end=None) -> pd.DataFrame:
    """Return genuine OHLCV or a distinct close-only NAV frame.

    This function never substitutes NAV for Open/High/Low/Volume. The caller
    must branch on ``frame.attrs['instrument_type']`` before OHLCV analysis.
    """
    if not eligible_for_fallback(ticker):
        return pd.DataFrame()
    import akshare as ak

    end_day = pd.Timestamp(end).date() if end is not None else date.today() + timedelta(days=1)
    start_day = pd.Timestamp(start).date() if start is not None else _start_date(period, end_day)
    if is_nav_symbol(ticker):
        code = FUND_PATTERN.fullmatch(str(ticker).strip()).group(1)
        raw = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        result = _normalize_nav(raw)
    else:
        code = mainland_code(ticker)
        window = dict(start_date=start_day.strftime("%Y%m%d"), end_date=end_day.strftime("%Y%m%d"), period="daily", adjust="qfq")
        # Exchange-traded funds have actual bars; do not confuse them with NAV.
        is_etf = code.startswith(("15", "16", "50", "51", "52", "56", "58"))
        if is_etf:
            try:
                raw = ak.fund_etf_hist_em(symbol=code, **window)
                result = _normalize_ohlcv(raw, source="ETF")
            except Exception:
                result = pd.DataFrame()
            if result.empty:
                suffix = str(ticker).upper().rsplit(".", 1)[-1] if "." in str(ticker) else ""
                exchange = "sh" if suffix == "SS" or (not suffix and code.startswith("5")) else "sz"
                raw = ak.fund_etf_hist_sina(symbol=f"{exchange}{code}")
                result = _normalize_ohlcv(raw, source="ETF_SINA")
        else:
            raw = ak.stock_zh_a_hist_em(symbol=code, **window)
            result = _normalize_ohlcv(raw, source="EQUITY")
    if result.empty:
        return result
    # The end bound is exclusive, matching yfinance's start/end convention.
    result = result.loc[(result.index.date >= start_day) & (result.index.date < end_day)]
    return result
