"""Pure calculations and SVG helpers for the multi-user short-term watchlist.

The module accepts only K-line payloads already returned by the existing API.
It deliberately does not fetch data, access Streamlit state, or persist account
configuration.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import time
from html import escape
from math import ceil, isfinite
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ticker_mapping import normalize_yfinance_ticker


SHORT_TERM_COLUMNS = (
    "Ticker", "Interval", "Price", "1D%", "Bar Diff%", "Candles (15)", "MA Spread‱", "MA 1 / MA 2",
    "Volume Ratio", "Volume (15)", "MACD Diff‱", "MACD / Signal", "Diff BB Upper%", "Diff VWAP%",
    "VWAP / Close", "RSI", "RSI (30/70)",
)

_DEFAULT_SETTINGS = {
    "ma_1": {"period": 9, "type": "EMA"},
    "ma_2": {"period": 21, "type": "EMA"},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "bollinger": {"period": 20, "stddev": 2.0},
    "rsi": {"period": 14},
}

_DEFAULT_ALERTS = {
    "enabled": False,
    "intervals": {"5m": True, "15m": True},
    "near_enabled": True,
    "confirmed_enabled": True,
    "duration_seconds": 15,
    "signals": {
        "macd": {"enabled": True, "threshold": 5.0},
        "ema": {"enabled": False, "threshold": 5.0},
        "bollinger": {"enabled": False, "threshold": 10.0},
        "vwap": {"enabled": False, "threshold": 5.0},
        "rsi": {"enabled": False, "threshold": 2.0},
    },
    "ticker_enabled": {},
}

_DEFAULT_SHORT_TERM_WATCHLIST = {
    "groups": {"Short-term": []},
    "settings": _DEFAULT_SETTINGS,
    "refresh": {"enabled": False, "interval_seconds": 10},
    "alerts": _DEFAULT_ALERTS,
}


def default_short_term_watchlist() -> dict[str, Any]:
    return deepcopy(_DEFAULT_SHORT_TERM_WATCHLIST)


def _bounded_int(value: Any, label: str, *, minimum: int = 1, maximum: int = 500) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _bounded_float(value: Any, label: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return number


def normalize_short_term_alerts(value: Any) -> dict[str, Any]:
    """Return a valid, backward-compatible MACD alert configuration."""
    source = value if isinstance(value, Mapping) else {}
    raw_intervals = source.get("intervals") if isinstance(source.get("intervals"), Mapping) else {}
    raw_signals = source.get("signals") if isinstance(source.get("signals"), Mapping) else {}
    legacy_macd_threshold = source.get("near_threshold_percent")
    signals = {}
    for signal_name, defaults in _DEFAULT_ALERTS["signals"].items():
        raw_signal = raw_signals.get(signal_name) if isinstance(raw_signals.get(signal_name), Mapping) else {}
        default_threshold = defaults["threshold"]
        if signal_name == "macd" and legacy_macd_threshold is not None and "threshold" not in raw_signal:
            try:
                default_threshold = float(legacy_macd_threshold) * 100.0
            except (TypeError, ValueError):
                pass
        try:
            threshold = _bounded_float(
                raw_signal.get("threshold", default_threshold), f"{signal_name} alert threshold", minimum=0.01, maximum=1000.0,
            )
        except (TypeError, ValueError):
            threshold = defaults["threshold"]
        signals[signal_name] = {
            "enabled": raw_signal.get("enabled") if isinstance(raw_signal.get("enabled"), bool) else defaults["enabled"],
            "threshold": threshold,
        }
    duration = source.get("duration_seconds", _DEFAULT_ALERTS["duration_seconds"])
    if duration not in {5, 10, 15, 30, 60}:
        duration = _DEFAULT_ALERTS["duration_seconds"]
    raw_ticker_enabled = source.get("ticker_enabled") if isinstance(source.get("ticker_enabled"), Mapping) else {}
    ticker_enabled = {
        normalize_yfinance_ticker(ticker): enabled
        for ticker, enabled in raw_ticker_enabled.items()
        if isinstance(enabled, bool) and normalize_yfinance_ticker(ticker)
    }
    return {
        "enabled": source.get("enabled") if isinstance(source.get("enabled"), bool) else _DEFAULT_ALERTS["enabled"],
        "intervals": {
            interval: raw_intervals.get(interval) if isinstance(raw_intervals.get(interval), bool) else _DEFAULT_ALERTS["intervals"][interval]
            for interval in ("5m", "15m")
        },
        "near_enabled": source.get("near_enabled") if isinstance(source.get("near_enabled"), bool) else _DEFAULT_ALERTS["near_enabled"],
        "confirmed_enabled": source.get("confirmed_enabled") if isinstance(source.get("confirmed_enabled"), bool) else _DEFAULT_ALERTS["confirmed_enabled"],
        "duration_seconds": duration,
        "signals": signals,
        "ticker_enabled": ticker_enabled,
    }


def normalize_short_term_watchlist(value: Any) -> dict[str, Any]:
    """Return a validated, backward-compatible account configuration."""
    source = value if isinstance(value, Mapping) else {}
    raw_groups = source.get("groups") if isinstance(source.get("groups"), Mapping) else {}
    groups: dict[str, list[str]] = {}
    for raw_name, raw_tickers in raw_groups.items():
        name = str(raw_name or "Group").strip()[:80] or "Group"
        tickers = []
        for raw_ticker in raw_tickers if isinstance(raw_tickers, (list, tuple)) else []:
            ticker = normalize_yfinance_ticker(raw_ticker)
            if ticker and ticker not in tickers:
                tickers.append(ticker)
        groups[name] = tickers
    if not groups:
        groups = deepcopy(_DEFAULT_SHORT_TERM_WATCHLIST["groups"])

    raw_settings = source.get("settings") if isinstance(source.get("settings"), Mapping) else {}
    merged = deepcopy(_DEFAULT_SETTINGS)
    for key in merged:
        if isinstance(raw_settings.get(key), Mapping):
            merged[key].update(raw_settings[key])
    try:
        ma_1_type = str(merged["ma_1"].get("type", "")).upper()
        ma_2_type = str(merged["ma_2"].get("type", "")).upper()
        if ma_1_type not in {"SMA", "EMA"} or ma_2_type not in {"SMA", "EMA"}:
            raise ValueError("Moving-average type must be SMA or EMA")
        settings = {
            "ma_1": {"period": _bounded_int(merged["ma_1"].get("period"), "MA 1 period"), "type": ma_1_type},
            "ma_2": {"period": _bounded_int(merged["ma_2"].get("period"), "MA 2 period"), "type": ma_2_type},
            "macd": {
                "fast": _bounded_int(merged["macd"].get("fast"), "MACD fast period"),
                "slow": _bounded_int(merged["macd"].get("slow"), "MACD slow period"),
                "signal": _bounded_int(merged["macd"].get("signal"), "MACD signal period"),
            },
            "bollinger": {
                "period": _bounded_int(merged["bollinger"].get("period"), "Bollinger period"),
                "stddev": _bounded_float(merged["bollinger"].get("stddev"), "Bollinger standard deviation", minimum=0.1, maximum=10.0),
            },
            "rsi": {"period": _bounded_int(merged["rsi"].get("period"), "RSI period")},
        }
        if settings["macd"]["fast"] >= settings["macd"]["slow"]:
            raise ValueError("MACD fast period must be smaller than slow period")
    except (TypeError, ValueError):
        settings = deepcopy(_DEFAULT_SETTINGS)
    raw_refresh = source.get("refresh") if isinstance(source.get("refresh"), Mapping) else {}
    refresh_enabled = raw_refresh.get("enabled", _DEFAULT_SHORT_TERM_WATCHLIST["refresh"]["enabled"])
    refresh_interval = raw_refresh.get("interval_seconds", _DEFAULT_SHORT_TERM_WATCHLIST["refresh"]["interval_seconds"])
    if not isinstance(refresh_enabled, bool):
        refresh_enabled = _DEFAULT_SHORT_TERM_WATCHLIST["refresh"]["enabled"]
    if refresh_interval not in {10, 20, 30}:
        refresh_interval = _DEFAULT_SHORT_TERM_WATCHLIST["refresh"]["interval_seconds"]
    return {
        "groups": groups,
        "settings": settings,
        "refresh": {"enabled": refresh_enabled, "interval_seconds": refresh_interval},
        "alerts": normalize_short_term_alerts(source.get("alerts")),
    }


def short_term_tickers(config: Mapping[str, Any]) -> list[str]:
    normalized = normalize_short_term_watchlist(config)
    return list(dict.fromkeys(ticker for tickers in normalized["groups"].values() for ticker in tickers))


def short_term_history_days(settings: Mapping[str, Any]) -> int:
    """Choose the smallest practical intraday history window for the active indicators.

    The 15-minute interval is the limiting one: a regular US trading day has
    roughly 26 bars.  Two days cover the defaults (including MACD 26/9), while
    longer user-selected windows request proportionally more history.
    """
    normalized = normalize_short_term_watchlist({"settings": settings})["settings"]
    required_bars = max(
        normalized["ma_1"]["period"],
        normalized["ma_2"]["period"],
        normalized["macd"]["slow"] + normalized["macd"]["signal"],
        normalized["bollinger"]["period"],
        normalized["rsi"]["period"] + 1,
    )
    return min(60, max(2, ceil(required_bars / 26)))


def _crossover_events(
    *, ticker: str, interval: str, timestamp: str, signal: str, label: str,
    current: Any, previous: Any, threshold: float, alerts: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Create at most one near/confirmed directional event for a zero-centered series."""
    try:
        current, previous = float(current), float(previous)
    except (TypeError, ValueError):
        return []
    if not all(isfinite(value) for value in (current, previous)):
        return []
    event_type = None
    if alerts["confirmed_enabled"]:
        if previous <= 0 < current:
            event_type = "bullish_confirmed"
        elif previous >= 0 > current:
            event_type = "bearish_confirmed"
    if event_type is None and alerts["near_enabled"] and abs(current) <= threshold:
        if current <= 0 and current > previous:
            event_type = "bullish_near"
        elif current >= 0 and current < previous:
            event_type = "bearish_near"
    if event_type is None:
        return []
    direction, state = event_type.split("_", 1)
    return [{
        "id": f"{ticker}|{interval}|{signal}|{event_type}|{timestamp}",
        "ticker": ticker,
        "interval": interval,
        "signal": signal,
        "type": event_type,
        "label": f"{label}: {direction.title()} crossover {state}",
        "timestamp": timestamp,
    }]


def short_term_alert_events(row: Mapping[str, Any], interval: str, alerts: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return enabled MACD, MA, Bollinger, VWAP, and RSI alert events for one row."""
    normalized = normalize_short_term_alerts(alerts)
    if not normalized["enabled"] or interval not in normalized["intervals"] or not normalized["intervals"][interval]:
        return []
    ticker = str(row.get("Ticker") or "").strip()
    timestamp = str(row.get("Alert Bar Timestamp") or "").strip()
    if not ticker or not timestamp or normalized["ticker_enabled"].get(ticker, True) is False:
        return []

    events = []
    signal_fields = {
        "macd": ("MACD Diff‱", "MACD Diff Previous‱", "MACD"),
        "ema": ("MA Cross (bp)", "MA Cross Previous (bp)", "MA 1 / MA 2"),
        "vwap": ("VWAP Cross (bp)", "VWAP Cross Previous (bp)", "VWAP / Close"),
        "rsi": ("RSI 30 Cross", "RSI 30 Cross Previous", "RSI 30"),
    }
    for signal, (current_key, previous_key, label) in signal_fields.items():
        config = normalized["signals"][signal]
        if config["enabled"]:
            events.extend(_crossover_events(
                ticker=ticker, interval=interval, timestamp=timestamp, signal=signal, label=label,
                current=row.get(current_key), previous=row.get(previous_key), threshold=config["threshold"], alerts=normalized,
            ))
    rsi_config = normalized["signals"]["rsi"]
    if rsi_config["enabled"]:
        events.extend(_crossover_events(
            ticker=ticker, interval=interval, timestamp=timestamp, signal="rsi_upper", label="RSI 70",
            current=row.get("RSI 70 Cross"), previous=row.get("RSI 70 Cross Previous"), threshold=rsi_config["threshold"], alerts=normalized,
        ))
    bb_config = normalized["signals"]["bollinger"]
    if bb_config["enabled"]:
        for signal, current_key, previous_key, label in (
            ("bollinger_upper", "BB Upper Cross (%)", "BB Upper Cross Previous (%)", "Bollinger upper"),
            ("bollinger_lower", "BB Lower Cross (%)", "BB Lower Cross Previous (%)", "Bollinger lower"),
        ):
            events.extend(_crossover_events(
                ticker=ticker, interval=interval, timestamp=timestamp, signal=signal, label=label,
                current=row.get(current_key), previous=row.get(previous_key), threshold=bb_config["threshold"], alerts=normalized,
            ))
    return events


def macd_alert_event(row: Mapping[str, Any], interval: str, alerts: Mapping[str, Any]) -> dict[str, str] | None:
    """Backward-compatible helper returning the first MACD event, if present."""
    return next((event for event in short_term_alert_events(row, interval, alerts) if event["signal"] == "macd"), None)


def consume_macd_alert_events(
    rows_by_pair: Mapping[tuple[str, str], Mapping[str, Any]],
    alerts: Mapping[str, Any],
    previous_state: Any,
    *,
    monitoring_enabled: bool,
    signal_signature: Any,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Return new alert events and a compact session-only de-duplication state."""
    candidates = []
    for (ticker, interval), row in rows_by_pair.items():
        candidates.extend(short_term_alert_events({"Ticker": ticker, **dict(row)}, interval, alerts))
    candidates.sort(key=lambda item: (item["interval"], item["ticker"], item["signal"], item["type"]))

    state = previous_state if isinstance(previous_state, Mapping) else {}
    seen = [str(value) for value in state.get("seen_event_ids", []) if isinstance(value, str)]
    candidate_ids = [event["id"] for event in candidates]
    same_signal_definition = state.get("signal_signature") == signal_signature
    initialized = bool(state.get("initialized")) and same_signal_definition
    if not initialized:
        return [], {
            "initialized": True,
            "signal_signature": signal_signature,
            "seen_event_ids": candidate_ids[-500:],
        }

    seen_set = set(seen)
    new_events = candidates if monitoring_enabled else []
    if monitoring_enabled:
        new_events = [event for event in candidates if event["id"] not in seen_set]
    merged_seen = list(dict.fromkeys([*seen, *candidate_ids]))[-500:]
    return new_events, {
        "initialized": True,
        "signal_signature": signal_signature,
        "seen_event_ids": merged_seen,
    }


def _percent(numerator: float, denominator: float) -> float:
    if not np.isfinite(numerator) or not np.isfinite(denominator) or denominator == 0:
        return np.nan
    return (numerator / denominator) * 100.0


def _basis_points(numerator: float, denominator: float) -> float:
    if not np.isfinite(numerator) or not np.isfinite(denominator) or denominator == 0:
        return np.nan
    return (numerator / denominator) * 10_000.0


def _path(values: list[float], width: int, height: int, padding: int = 3, domain: tuple[float, float] | None = None) -> str:
    finite = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if not len(finite):
        return ""
    low, high = domain if domain is not None else (float(finite.min()), float(finite.max()))
    spread = high - low or max(abs(high), 1.0) * 0.02
    pieces = []
    denominator = max(len(values) - 1, 1)
    for index, value in enumerate(values):
        if not np.isfinite(value):
            continue
        x = padding + (width - padding * 2) * index / denominator
        y = height - padding - (height - padding * 2) * (value - low) / spread
        pieces.append(("M" if not pieces else "L") + f"{x:.1f},{y:.1f}")
    return " ".join(pieces)


def candlestick_svg(
    open_values: list[float], high_values: list[float], low_values: list[float], close_values: list[float],
    *, width: int = 80, height: int = 30,
) -> str:
    """Return an inline SVG for the supplied candles, with no untrusted markup."""
    width, height, padding = max(int(width), 16), max(int(height), 16), 2
    if not open_values or not (len(open_values) == len(high_values) == len(low_values) == len(close_values)):
        return ""
    low = np.nanmin(np.asarray(low_values, dtype=float))
    high = np.nanmax(np.asarray(high_values, dtype=float))
    if not np.isfinite(low) or not np.isfinite(high):
        return ""
    spread = high - low or max(abs(high), 1.0) * 0.02
    step = (width - padding * 2) / max(len(open_values), 1)
    rect_width = max(1.5, step * 0.54)

    def y(value: float) -> float:
        return height - padding - (height - padding * 2) * (value - low) / spread

    pieces = []
    for index, (open_price, high_price, low_price, close_price) in enumerate(zip(open_values, high_values, low_values, close_values)):
        if not all(np.isfinite(value) for value in (open_price, high_price, low_price, close_price)):
            continue
        x = padding + step * (index + 0.5)
        color = "#26a69a" if close_price >= open_price else "#ef5350"
        top, bottom = sorted((y(open_price), y(close_price)))
        body_height = max(1.0, bottom - top)
        pieces.append(f"<line x1='{x:.1f}' y1='{y(high_price):.1f}' x2='{x:.1f}' y2='{y(low_price):.1f}' stroke='{color}' stroke-width='1'/>")
        pieces.append(f"<rect x='{x - rect_width / 2:.1f}' y='{top:.1f}' width='{rect_width:.1f}' height='{body_height:.1f}' fill='{color}'/>")
    return f"<svg viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='Last {len(open_values)} candles'>{''.join(pieces)}</svg>"


def two_line_svg(
    first_values: list[float],
    second_values: list[float],
    *,
    first_color: str,
    second_color: str,
    label: str,
    reference_lines: tuple[float, ...] = (),
) -> str:
    width, height = 80, 30
    combined = np.asarray(
        [value for value in first_values + second_values + list(reference_lines) if np.isfinite(value)],
        dtype=float,
    )
    if not len(combined):
        return ""
    domain = (float(combined.min()), float(combined.max()))
    first_path = _path(first_values, width, height, domain=domain)
    second_path = _path(second_values, width, height, domain=domain)
    if not first_path and not second_path:
        return ""
    low, high = domain
    spread = high - low or max(abs(high), 1.0) * 0.02
    reference_svg = ""
    for reference in reference_lines:
        if not np.isfinite(reference):
            continue
        y = height - 3 - (height - 6) * (reference - low) / spread
        reference_svg += (
            f"<line x1='2' y1='{y:.1f}' x2='{width - 2}' y2='{y:.1f}' "
            "stroke='#94a3b8' stroke-width='0.8' stroke-dasharray='3 2'/>"
        )
    return (
        f"<svg viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='{escape(label, quote=True)}'>"
        f"{reference_svg}"
        f"<path d='{escape(first_path, quote=True)}' fill='none' stroke='{first_color}' stroke-width='1.6'/>"
        f"<path d='{escape(second_path, quote=True)}' fill='none' stroke='{second_color}' stroke-width='1.4'/>"
        "</svg>"
    )


def macd_svg(macd_values: list[float], signal_values: list[float]) -> str:
    return two_line_svg(
        macd_values, signal_values,
        first_color="#2563eb", second_color="#f59e0b", label="MACD and signal", reference_lines=(0.0,),
    )


def vwap_svg(close_values: list[float], vwap_values: list[float]) -> str:
    return two_line_svg(
        close_values, vwap_values,
        first_color="#2563eb", second_color="#0f766e", label="Close and VWAP",
    )


def rsi_svg(rsi_values: list[float]) -> str:
    return two_line_svg(
        rsi_values, [], first_color="#7c3aed", second_color="#7c3aed", label="RSI with 30 and 70 levels",
        reference_lines=(30.0, 70.0),
    )


def volume_svg(volume_values: list[float]) -> str:
    """Return a compact bar sparkline for the last 15 K-line volumes."""
    width, height, padding = 80, 30, 2
    values = np.asarray(volume_values, dtype=float)
    finite = values[np.isfinite(values) & (values >= 0)]
    if not len(finite):
        return ""
    maximum = max(float(finite.max()), 1.0)
    step = (width - padding * 2) / max(len(values), 1)
    bar_width = max(1.0, step * 0.65)
    pieces = []
    for index, value in enumerate(values):
        if not np.isfinite(value) or value < 0:
            continue
        bar_height = max(0.8, (height - padding * 2) * float(value) / maximum)
        x = padding + step * index + (step - bar_width) / 2
        y = height - padding - bar_height
        pieces.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bar_width:.1f}' height='{bar_height:.1f}' fill='#60a5fa'/>")
    return f"<svg viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='Last 15 volumes'>{''.join(pieces)}</svg>"


def _ny_timestamps(dates: list[Any]) -> pd.Series:
    parsed = pd.to_datetime(pd.Series(dates), errors="coerce")
    if getattr(parsed.dt, "tz", None) is None:
        return parsed.dt.tz_localize("America/New_York")
    return parsed.dt.tz_convert("America/New_York")


def _is_continuous_market(ticker: str) -> bool:
    return str(ticker).upper().endswith("-USD") or str(ticker).upper().endswith("=X")


def _price_source(ticker: str, timestamp: pd.Timestamp, volume: float) -> str:
    if _is_continuous_market(ticker):
        return "Regular"
    if pd.isna(timestamp):
        return "Unknown"
    local_time = timestamp.timetz().replace(tzinfo=None)
    if timestamp.weekday() >= 5:
        return "Off-hours"
    if time(9, 30) <= local_time < time(16, 0):
        return "Regular"
    if local_time < time(9, 30):
        return "Pre-market"
    if local_time >= time(16, 0):
        return "After-hours"
    return "Off-hours" if not volume else "Unknown"


def calculate_short_term_row(ticker: str, kline_data: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate one interval row from an existing /api/kline_data response."""
    normalized = normalize_short_term_watchlist({"settings": settings})["settings"]
    ohlc = kline_data.get("ohlc", {}) if isinstance(kline_data, Mapping) else {}
    dates = list(kline_data.get("dates") or []) if isinstance(kline_data, Mapping) else []
    required = {key: list(ohlc.get(key) or []) for key in ("open", "high", "low", "close", "volume")}
    length = len(dates)
    if length < 2 or any(len(values) != length for values in required.values()):
        return {"Ticker": ticker, "Error": "Insufficient intraday data"}

    frame = pd.DataFrame({"date": dates, **required})
    for key in required:
        frame[key] = pd.to_numeric(frame[key], errors="coerce").replace([np.inf, -np.inf], np.nan)
    frame["timestamp"] = _ny_timestamps(dates)
    if frame[["open", "high", "low", "close"]].iloc[-1].isna().any():
        return {"Ticker": ticker, "Error": "Latest intraday bar is invalid"}

    close = frame["close"]
    latest_price = float(close.iloc[-1])
    previous_close = float(close.iloc[-2]) if pd.notna(close.iloc[-2]) else np.nan
    ma_series = []
    for key in ("ma_1", "ma_2"):
        item = normalized[key]
        series = close.ewm(span=item["period"], adjust=False).mean() if item["type"] == "EMA" else close.rolling(item["period"], min_periods=item["period"]).mean()
        ma_series.append(series)

    macd_settings = normalized["macd"]
    macd = close.ewm(span=macd_settings["fast"], adjust=False).mean() - close.ewm(span=macd_settings["slow"], adjust=False).mean()
    signal = macd.ewm(span=macd_settings["signal"], adjust=False).mean()
    bb_settings = normalized["bollinger"]
    bb_mid = close.rolling(bb_settings["period"], min_periods=bb_settings["period"]).mean()
    bb_std = close.rolling(bb_settings["period"], min_periods=bb_settings["period"]).std()
    bb_upper = bb_mid + bb_settings["stddev"] * bb_std
    bb_lower = bb_mid - bb_settings["stddev"] * bb_std
    rsi_period = normalized["rsi"]["period"]
    delta = close.diff()
    gains = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    losses = -delta.where(delta < 0, 0).rolling(rsi_period).mean()
    rsi = 100 - (100 / (1 + gains / losses))

    latest_timestamp = frame["timestamp"].iloc[-1]
    latest_volume = float(frame["volume"].iloc[-1]) if pd.notna(frame["volume"].iloc[-1]) else np.nan
    if _is_continuous_market(ticker):
        regular = frame["timestamp"].notna()
    else:
        regular = frame["timestamp"].map(
            lambda stamp: pd.notna(stamp) and stamp.weekday() < 5 and time(9, 30) <= stamp.timetz().replace(tzinfo=None) < time(16, 0)
        )
    vwap_mask = regular & frame["volume"].gt(0) & frame[["high", "low", "close"]].notna().all(axis=1)
    vwap_series = pd.Series(np.nan, index=frame.index, dtype=float)
    if vwap_mask.any():
        eligible_dates = frame.loc[vwap_mask, "timestamp"].dt.date
        for _, row_indexes in eligible_dates.groupby(eligible_dates).groups.items():
            typical = (frame.loc[row_indexes, "high"] + frame.loc[row_indexes, "low"] + frame.loc[row_indexes, "close"]) / 3
            volume = frame.loc[row_indexes, "volume"]
            vwap_series.loc[row_indexes] = (typical * volume).cumsum() / volume.cumsum()
    vwap = float(vwap_series.iloc[-1]) if pd.notna(vwap_series.iloc[-1]) else np.nan
    latest_regular = bool(regular.iloc[-1]) if len(regular) else False
    vwap_diff = _percent(latest_price - vwap, vwap) if latest_regular and pd.notna(latest_volume) and latest_volume > 0 else np.nan
    volume_ema = frame["volume"].ewm(span=5, adjust=False).mean()
    macd_diff = macd - signal
    previous_vwap = float(vwap_series.iloc[-2]) if pd.notna(vwap_series.iloc[-2]) else np.nan

    def _cross_bps(current_value, previous_value, current_reference, previous_reference):
        return (
            _basis_points(float(current_value) - float(current_reference), float(current_reference)),
            _basis_points(float(previous_value) - float(previous_reference), float(previous_reference)),
        )

    ma_cross, ma_cross_previous = _cross_bps(
        ma_series[0].iloc[-1], ma_series[0].iloc[-2], ma_series[1].iloc[-1], ma_series[1].iloc[-2],
    )
    def _band_width_percent(price, boundary, upper, lower):
        return _percent(float(price) - float(boundary), float(upper) - float(lower))

    bb_upper_cross = _band_width_percent(latest_price, bb_upper.iloc[-1], bb_upper.iloc[-1], bb_lower.iloc[-1])
    bb_upper_cross_previous = _band_width_percent(previous_close, bb_upper.iloc[-2], bb_upper.iloc[-2], bb_lower.iloc[-2])
    bb_lower_cross = _band_width_percent(latest_price, bb_lower.iloc[-1], bb_upper.iloc[-1], bb_lower.iloc[-1])
    bb_lower_cross_previous = _band_width_percent(previous_close, bb_lower.iloc[-2], bb_upper.iloc[-2], bb_lower.iloc[-2])
    vwap_cross, vwap_cross_previous = _cross_bps(latest_price, previous_close, vwap, previous_vwap)

    tail = frame.iloc[-15:]
    return {
        "Ticker": ticker,
        "Price": latest_price,
        "Price Source": _price_source(ticker, latest_timestamp, latest_volume),
        "Bar Diff%": _percent(latest_price - previous_close, previous_close),
        "Candles (15)": candlestick_svg(tail["open"].tolist(), tail["high"].tolist(), tail["low"].tolist(), tail["close"].tolist()),
        "MA Spread‱": _basis_points(float(ma_series[0].iloc[-1]) - float(ma_series[1].iloc[-1]), float(ma_series[1].iloc[-1])) if pd.notna(ma_series[0].iloc[-1]) and pd.notna(ma_series[1].iloc[-1]) else np.nan,
        "MA 1 / MA 2": two_line_svg(
            ma_series[0].iloc[-15:].tolist(), ma_series[1].iloc[-15:].tolist(),
            first_color="#16a34a", second_color="#9333ea", label="Moving averages",
        ),
        "Volume Ratio": float(latest_volume / volume_ema.iloc[-1]) if pd.notna(latest_volume) and pd.notna(volume_ema.iloc[-1]) and volume_ema.iloc[-1] > 0 else np.nan,
        "Volume (15)": volume_svg(tail["volume"].tolist()),
        "MACD Diff‱": _basis_points(float(macd_diff.iloc[-1]), latest_price) if pd.notna(macd_diff.iloc[-1]) else np.nan,
        "MACD Diff Previous‱": _basis_points(float(macd_diff.iloc[-2]), previous_close) if pd.notna(macd_diff.iloc[-2]) else np.nan,
        "MA Cross (bp)": ma_cross,
        "MA Cross Previous (bp)": ma_cross_previous,
        "BB Upper Cross (%)": bb_upper_cross,
        "BB Upper Cross Previous (%)": bb_upper_cross_previous,
        "BB Lower Cross (%)": bb_lower_cross,
        "BB Lower Cross Previous (%)": bb_lower_cross_previous,
        "VWAP Cross (bp)": vwap_cross,
        "VWAP Cross Previous (bp)": vwap_cross_previous,
        "RSI 30 Cross": float(rsi.iloc[-1] - 30.0) if pd.notna(rsi.iloc[-1]) else np.nan,
        "RSI 30 Cross Previous": float(rsi.iloc[-2] - 30.0) if pd.notna(rsi.iloc[-2]) else np.nan,
        "RSI 70 Cross": float(rsi.iloc[-1] - 70.0) if pd.notna(rsi.iloc[-1]) else np.nan,
        "RSI 70 Cross Previous": float(rsi.iloc[-2] - 70.0) if pd.notna(rsi.iloc[-2]) else np.nan,
        "Alert Bar Timestamp": str(dates[-1]),
        "MACD / Signal": macd_svg(macd.iloc[-15:].tolist(), signal.iloc[-15:].tolist()),
        "Diff BB Upper%": _percent(latest_price - float(bb_upper.iloc[-1]), float(bb_upper.iloc[-1] - bb_lower.iloc[-1])) if pd.notna(bb_upper.iloc[-1]) and pd.notna(bb_lower.iloc[-1]) else np.nan,
        "Diff VWAP%": vwap_diff,
        "VWAP / Close": vwap_svg(tail["close"].tolist(), vwap_series.iloc[-15:].tolist()),
        "RSI": float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else np.nan,
        "RSI (30/70)": rsi_svg(rsi.iloc[-15:].tolist()),
    }
