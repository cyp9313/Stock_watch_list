"""Pure helpers for option open-interest wall payloads.

The market-data backend owns yfinance calls.  Keeping parsing and aggregation
here makes the provider response easy to validate and test without network IO.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


def select_option_expirations(expirations: Sequence[Any], as_of: dt.date, months: int = 3) -> tuple[str | None, list[str], str]:
    """Return the nearest available expiry and all expiries through ``months`` ahead."""
    if isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= 12:
        raise ValueError("Option-wall horizon must be between 1 and 12 months")
    parsed: list[dt.date] = []
    for value in expirations:
        timestamp = pd.to_datetime(value, errors="coerce")
        if pd.notna(timestamp):
            parsed.append(timestamp.date())
    available = sorted(set(expiry for expiry in parsed if expiry >= as_of))
    if not available:
        return None, [], (pd.Timestamp(as_of) + pd.DateOffset(months=3)).date().isoformat()
    cutoff = (pd.Timestamp(as_of) + pd.DateOffset(months=months)).date()
    return available[0].isoformat(), [expiry.isoformat() for expiry in available if expiry <= cutoff], cutoff.isoformat()


def _chain_sides(chain: Any) -> tuple[Any, Any]:
    if isinstance(chain, Mapping):
        return chain.get("calls"), chain.get("puts")
    return getattr(chain, "calls", None), getattr(chain, "puts", None)


def _side_open_interest(frame: Any) -> pd.Series:
    if not isinstance(frame, pd.DataFrame) or frame.empty or not {"strike", "openInterest"}.issubset(frame.columns):
        return pd.Series(dtype="float64")
    parsed = pd.DataFrame({
        "strike": pd.to_numeric(frame["strike"], errors="coerce"),
        "open_interest": pd.to_numeric(frame["openInterest"], errors="coerce"),
    })
    parsed = parsed.replace([np.inf, -np.inf], np.nan).dropna()
    parsed = parsed[(parsed["strike"] > 0) & (parsed["open_interest"] >= 0)]
    if parsed.empty:
        return pd.Series(dtype="float64")
    return parsed.groupby("strike", sort=True)["open_interest"].sum()


def aggregate_open_interest(chains: Iterable[Any]) -> list[dict[str, float | int]]:
    """Aggregate call/put OI by strike for one or many expiry chains."""
    calls = pd.Series(dtype="float64")
    puts = pd.Series(dtype="float64")
    for chain in chains:
        call_frame, put_frame = _chain_sides(chain)
        calls = calls.add(_side_open_interest(call_frame), fill_value=0.0)
        puts = puts.add(_side_open_interest(put_frame), fill_value=0.0)
    strikes = sorted(set(calls.index).union(puts.index))
    return [
        {
            "strike": float(strike),
            "calls": int(round(float(calls.get(strike, 0.0)))),
            "puts": int(round(float(puts.get(strike, 0.0)))),
        }
        for strike in strikes
        if float(calls.get(strike, 0.0)) > 0 or float(puts.get(strike, 0.0)) > 0
    ]


def option_gamma_legs(chain: Any, expiration: str) -> list[dict[str, float | int | str]]:
    """Extract the minimal, JSON-safe contract inputs needed to recalculate GEX.

    The caller deliberately retains IV/OI rather than a precomputed Gamma so a
    refreshed underlying price can update the chart without downloading a new
    option chain.
    """
    try:
        expiry = pd.Timestamp(expiration).date().isoformat()
    except (TypeError, ValueError):
        return []
    call_frame, put_frame = _chain_sides(chain)
    legs: list[dict[str, float | int | str]] = []
    for side, frame in (("call", call_frame), ("put", put_frame)):
        required = {"strike", "openInterest", "impliedVolatility"}
        if not isinstance(frame, pd.DataFrame) or frame.empty or not required.issubset(frame.columns):
            continue
        parsed = pd.DataFrame({
            "strike": pd.to_numeric(frame["strike"], errors="coerce"),
            "open_interest": pd.to_numeric(frame["openInterest"], errors="coerce"),
            "implied_volatility": pd.to_numeric(frame["impliedVolatility"], errors="coerce"),
        }).replace([np.inf, -np.inf], np.nan).dropna()
        parsed = parsed[
            (parsed["strike"] > 0)
            & (parsed["open_interest"] > 0)
            & (parsed["implied_volatility"] > 0)
        ]
        for row in parsed.itertuples(index=False):
            legs.append({
                "expiration": expiry,
                "side": side,
                "strike": float(row.strike),
                "open_interest": int(round(float(row.open_interest))),
                "implied_volatility": float(row.implied_volatility),
                "multiplier": 100,
            })
    return legs


def calculate_dealer_gex(
    legs: Iterable[Any],
    spot: Any,
    risk_free_rate: Any = 0.0,
    dividend_yield: Any = 0.0,
    now: dt.datetime | None = None,
) -> list[dict[str, float]]:
    """Aggregate an estimated Dealer-GEX profile by strike.

    Calls are positive and puts negative, which is a transparent proxy for the
    common assumption that dealers are the other side of customer option flow.
    GEX is quoted as the estimated dollar change for a one-percent underlying
    price move.  Each contract keeps its own expiry and IV before aggregation.
    """
    try:
        current_spot = float(spot)
        rate = float(risk_free_rate)
        yield_rate = float(dividend_yield)
    except (TypeError, ValueError):
        return []
    if not (np.isfinite(current_spot) and current_spot > 0 and np.isfinite(rate) and np.isfinite(yield_rate)):
        return []
    eastern = ZoneInfo("America/New_York")
    current_time = now or dt.datetime.now(eastern)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=eastern)
    else:
        current_time = current_time.astimezone(eastern)

    totals: dict[float, dict[str, float]] = {}
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        try:
            expiry_date = pd.Timestamp(leg.get("expiration")).date()
            strike = float(leg.get("strike"))
            open_interest = float(leg.get("open_interest"))
            volatility = float(leg.get("implied_volatility"))
            multiplier = float(leg.get("multiplier", 100))
        except (TypeError, ValueError):
            continue
        side = str(leg.get("side", "")).lower()
        if (
            side not in {"call", "put"}
            or not all(np.isfinite(value) for value in (strike, open_interest, volatility, multiplier))
            or strike <= 0 or open_interest <= 0 or volatility <= 0 or multiplier <= 0
        ):
            continue
        expiration_time = dt.datetime.combine(expiry_date, dt.time(16, 0), tzinfo=eastern)
        seconds_to_expiry = max((expiration_time - current_time).total_seconds(), 60.0)
        years_to_expiry = seconds_to_expiry / (365.25 * 24 * 60 * 60)
        root_time = np.sqrt(years_to_expiry)
        d1 = (np.log(current_spot / strike) + (rate - yield_rate + 0.5 * volatility ** 2) * years_to_expiry) / (volatility * root_time)
        gamma = float(np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * np.pi) / (current_spot * volatility * root_time))
        gross_gex = gamma * open_interest * multiplier * current_spot ** 2 * 0.01
        bucket = totals.setdefault(strike, {"call_gex": 0.0, "put_gex": 0.0})
        bucket["call_gex" if side == "call" else "put_gex"] += gross_gex

    return [
        {
            "strike": float(strike),
            "call_gex": values["call_gex"],
            "put_gex": -values["put_gex"],
            "dealer_gex": values["call_gex"] - values["put_gex"],
        }
        for strike, values in sorted(totals.items())
    ]
