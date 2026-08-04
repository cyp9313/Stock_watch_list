"""Deterministic, cash-aware dollar-cost-averaging portfolio backtests.

The module deliberately has no Streamlit, Flask, yfinance or SQLite dependency so
the calculation can be regression-tested with synthetic adjusted-close data.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import numpy as np
import pandas as pd


BENCHMARK_TICKERS = ("SPY", "QQQ")
MAX_LOOKAHEAD_DAYS = 10


def normalize_backtest_settings(frequency: str, monthly_timing: str) -> tuple[str, str]:
    """Return supported cadence values without silently changing financial rules."""
    frequency = str(frequency or "monthly").strip().lower()
    monthly_timing = str(monthly_timing or "start").strip().lower()
    if frequency not in {"weekly", "monthly"}:
        frequency = "monthly"
    if monthly_timing not in {"start", "middle"}:
        monthly_timing = "start"
    return frequency, monthly_timing


def _as_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_localize(None)
    return timestamp.normalize()


def scheduled_contribution_dates(start_date, end_date, frequency="monthly", monthly_timing="start") -> list[pd.Timestamp]:
    """Build weekly Friday or monthly (day 1 / third Friday) contribution dates."""
    start, end = _as_timestamp(start_date), _as_timestamp(end_date)
    if start > end:
        return []
    frequency, monthly_timing = normalize_backtest_settings(frequency, monthly_timing)
    if frequency == "weekly":
        first = start + pd.Timedelta(days=(4 - start.weekday()) % 7)
        return list(pd.date_range(first, end, freq="W-FRI"))

    if monthly_timing == "start":
        first = start.replace(day=1)
        if first < start:
            first = start + pd.offsets.MonthBegin(1)
        return list(pd.date_range(first, end, freq=pd.DateOffset(months=1)))

    # The third calendar Friday matches the usual US monthly options-expiration
    # convention (with the normal next-trading-day roll handled by the caller).
    cursor = start.replace(day=1)
    dates = []
    while cursor <= end:
        first_friday = cursor + pd.Timedelta(days=(4 - cursor.weekday()) % 7)
        third_friday = first_friday + pd.Timedelta(days=14)
        if start <= third_friday <= end:
            dates.append(third_friday)
        cursor = cursor + pd.offsets.MonthBegin(1)
    return dates


def _clean_prices(prices: pd.DataFrame, tickers: Iterable[str]) -> pd.DataFrame:
    columns = [str(ticker) for ticker in tickers]
    frame = pd.DataFrame(prices).copy()
    frame.columns = [str(column) for column in frame.columns]
    frame = frame.reindex(columns=columns)
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame[~frame.index.isna()]
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_localize(None)
    frame.index = frame.index.normalize()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame.apply(pd.to_numeric, errors="coerce").where(lambda value: value > 0)


def _next_price_date(series: pd.Series, scheduled_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.Timestamp | None:
    latest = min(end_date, scheduled_date + pd.Timedelta(days=MAX_LOOKAHEAD_DAYS))
    candidates = series.loc[(series.index >= scheduled_date) & (series.index <= latest)].dropna()
    return candidates.index[0] if not candidates.empty else None


def _run_single_dca(
    prices: pd.Series,
    timeline: pd.DatetimeIndex,
    scheduled_dates: list[pd.Timestamp],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> tuple[pd.Series, list[dict], int]:
    """Simulate one benchmark using the portfolio's actual contribution dates."""
    prices = pd.to_numeric(prices, errors="coerce").where(lambda value: value > 0)
    prices = prices.loc[(prices.index >= start_date) & (prices.index <= end_date)]
    last_price = prices.reindex(timeline).ffill()
    holdings = 0.0
    cash = 0.0
    contribution_count = 0
    events_by_day: dict[pd.Timestamp, list[tuple[pd.Timestamp, float]]] = {}
    for scheduled in scheduled_dates:
        if scheduled > end_date:
            continue
        contribution_count += 1
        trade_date = _next_price_date(prices, scheduled, end_date)
        if trade_date is not None:
            events_by_day.setdefault(trade_date, []).append((scheduled, 1.0))

    values = []
    events = []
    contribution_dates = set(scheduled_dates)
    for current_date, current_price in last_price.items():
        if current_date in contribution_dates:
            cash += 1.0
        for scheduled, amount in events_by_day.get(current_date, []):
            holdings += amount / float(current_price)
            cash -= amount
            events.append({
                "scheduled_date": scheduled.date().isoformat(),
                "trade_date": current_date.date().isoformat(),
                "amount": amount,
            })
        values.append(cash + (holdings * float(current_price) if pd.notna(current_price) else 0.0))
    return pd.Series(values, index=timeline, dtype=float), events, contribution_count


def run_equal_weight_dca_backtest(
    prices: pd.DataFrame,
    portfolio_tickers: Iterable[str],
    start_date,
    end_date,
    frequency="monthly",
    monthly_timing="start",
    benchmarks: Iterable[str] = BENCHMARK_TICKERS,
) -> dict:
    """Backtest equal-weight DCA with adjusted closes and per-ticker execution dates.

    One unit of cash is contributed on each executable scheduled date. On that
    date the unit is split equally only across tickers that already have price
    history and can trade within ten calendar days. A ticker that has not yet
    listed therefore receives no fictional pre-IPO allocation; it joins the
    equal-weight universe on the next eligible DCA date after listing.
    """
    start, end = _as_timestamp(start_date), _as_timestamp(end_date)
    if start >= end:
        raise ValueError("The backtest end date must be after the start date.")
    frequency, monthly_timing = normalize_backtest_settings(frequency, monthly_timing)
    portfolio_tickers = list(dict.fromkeys(str(ticker).strip() for ticker in portfolio_tickers if str(ticker).strip()))
    benchmark_tickers = list(dict.fromkeys(str(ticker).strip() for ticker in benchmarks if str(ticker).strip()))
    if not portfolio_tickers:
        raise ValueError("Add at least one portfolio holding before running a DCA backtest.")

    source = _clean_prices(prices, portfolio_tickers + benchmark_tickers)
    source = source.loc[source.index <= end]
    window_source = source.loc[source.index >= start]
    if window_source.empty:
        raise ValueError("No adjusted-close history is available in the selected date range.")
    usable_tickers = [
        ticker for ticker in portfolio_tickers
        if ticker in window_source and window_source[ticker].notna().any()
    ]
    unavailable_tickers = [ticker for ticker in portfolio_tickers if ticker not in usable_tickers]
    if not usable_tickers:
        raise ValueError("None of the portfolio holdings has usable adjusted-close history in this date range.")

    scheduled_dates = scheduled_contribution_dates(start, end, frequency, monthly_timing)
    initial_contribution_fallback = not scheduled_dates
    if initial_contribution_fallback:
        # A short selected range can sit wholly between two Friday schedules.
        # Treat its chosen start as one explicit initial investment rather than
        # rejecting an otherwise valid one-period backtest.
        scheduled_dates = [start]
    if not scheduled_dates:
        raise ValueError("The selected date range contains no scheduled DCA contributions.")
    timeline = pd.date_range(start, end, freq="D")

    # Build each period's tradable universe before allocating its contribution.
    # A first-ever price after the scheduled date indicates a later IPO/listing,
    # so it is deliberately excluded until the following DCA date. Existing
    # instruments may still roll a holiday/suspension execution forward.
    orders_by_day: dict[pd.Timestamp, list[dict]] = {}
    active_contribution_dates: list[pd.Timestamp] = []
    skipped_contribution_dates: list[pd.Timestamp] = []
    for scheduled in scheduled_dates:
        executable = []
        for ticker in usable_tickers:
            series = source[ticker]
            if series.loc[series.index <= scheduled].dropna().empty:
                continue
            trade_date = _next_price_date(series, scheduled, end)
            if trade_date is not None:
                executable.append((ticker, trade_date))
        if not executable:
            skipped_contribution_dates.append(scheduled)
            continue
        active_contribution_dates.append(scheduled)
        allocation = 1.0 / len(executable)
        for ticker, trade_date in executable:
            orders_by_day.setdefault(trade_date, []).append({
                "ticker": ticker,
                "scheduled_date": scheduled,
                "amount": allocation,
            })

    if not active_contribution_dates:
        raise ValueError("No portfolio holdings were tradable on any scheduled DCA date in this range.")

    last_prices = source[usable_tickers].reindex(timeline).ffill()
    holdings = {ticker: 0.0 for ticker in usable_tickers}
    cash = 0.0
    portfolio_values = []
    all_events = []
    active_dates_set = set(active_contribution_dates)
    for current_date in timeline:
        if current_date in active_dates_set:
            cash += 1.0
        for order in orders_by_day.get(current_date, []):
            ticker = order["ticker"]
            price = last_prices.at[current_date, ticker]
            if pd.isna(price) or price <= 0:
                continue
            holdings[ticker] += order["amount"] / float(price)
            cash -= order["amount"]
            all_events.append({
                "ticker": ticker,
                "scheduled_date": order["scheduled_date"].date().isoformat(),
                "trade_date": current_date.date().isoformat(),
                "amount": order["amount"],
            })
        market_value = sum(
            holdings[ticker] * float(last_prices.at[current_date, ticker])
            for ticker in usable_tickers
            if pd.notna(last_prices.at[current_date, ticker])
        )
        portfolio_values.append(cash + market_value)
    portfolio_value = pd.Series(portfolio_values, index=timeline, dtype=float)

    contribution_total = float(len(active_contribution_dates))
    contributed = pd.Series(
        [sum(1 for scheduled in active_contribution_dates if scheduled <= current_date) for current_date in timeline],
        index=timeline,
        dtype=float,
    )
    portfolio_return = (portfolio_value / contributed.replace(0, np.nan) - 1.0) * 100.0

    curves = [{
        "key": "portfolio",
        "label": "Equal-weight portfolio",
        "dates": [item.date().isoformat() for item in timeline],
        "return_pct": [round(float(value), 4) if pd.notna(value) else None for value in portfolio_return],
    }]
    benchmark_status = {}
    for ticker in benchmark_tickers:
        if ticker not in source or not source[ticker].notna().any():
            benchmark_status[ticker] = "No adjusted-close data available."
            continue
        value, _, _ = _run_single_dca(source[ticker], timeline, active_contribution_dates, start, end)
        return_pct = (value / contributed.replace(0, np.nan) - 1.0) * 100.0
        curves.append({
            "key": ticker,
            "label": ticker,
            "dates": [item.date().isoformat() for item in timeline],
            "return_pct": [round(float(item), 4) if pd.notna(item) else None for item in return_pct],
        })
        benchmark_status[ticker] = "ok"

    # A contribution can execute on different local-market days for different
    # tickers.  Draw one marker per scheduled portfolio contribution instead of
    # one marker per individual fill, so monthly DCA visibly remains monthly.
    marker_dates = active_contribution_dates
    marker_lookup = portfolio_return.to_dict()
    return {
        "success": True,
        "frequency": frequency,
        "monthly_timing": monthly_timing,
        "initial_contribution_fallback": initial_contribution_fallback,
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "portfolio_tickers": usable_tickers,
        "unavailable_tickers": unavailable_tickers,
        "scheduled_contributions": len(scheduled_dates),
        "executed_contributions": len(active_contribution_dates),
        "skipped_contributions": len(skipped_contribution_dates),
        "contribution_total": contribution_total,
        "curves": curves,
        "buy_markers": [
            {
                "date": marker.date().isoformat(),
                "return_pct": round(float(marker_lookup[marker]), 4) if pd.notna(marker_lookup.get(marker)) else None,
            }
            for marker in marker_dates
        ],
        "trade_events": all_events,
        "benchmark_status": benchmark_status,
    }
