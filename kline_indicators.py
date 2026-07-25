"""Configurable technical indicators used by the Streamlit K-line charts.

The module intentionally operates only on already downloaded OHLCV data.  It
keeps the K-line HTTP API backwards compatible while allowing each browser to
recalculate chart-only indicators with its own settings.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


MA_DEFAULT_PERIODS = (5, 10, 20, 50, 100, 200)
INTRADAY_INTERVALS = frozenset({"5m", "15m", "1h", "4h"})
MAX_PERIOD = 10_000

_DEFAULT_INDICATOR_SETTINGS = {
    "moving_averages": [
        {"period": period, "type": "SMA"}
        for period in MA_DEFAULT_PERIODS
    ],
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "kdj": {"period": 9, "k_smoothing": 3, "d_smoothing": 3},
    "rsi": {"period": 14},
    "fibonacci": {
        "retracement": {"enabled": False, "deviation": 3.0, "depth": 10},
        "extension": {"enabled": False, "depth": 10},
    },
}


def default_indicator_settings() -> dict[str, Any]:
    """Return a new copy of the chart's backward-compatible defaults."""
    return deepcopy(_DEFAULT_INDICATOR_SETTINGS)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a positive integer")
    if not 1 <= value <= MAX_PERIOD:
        raise ValueError(f"{label} must be between 1 and {MAX_PERIOD}")
    return value


def _bounded_depth(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not 2 <= value <= 500:
        raise ValueError(f"{label} must be between 2 and 500")
    return value


def _bounded_deviation(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Retracement deviation must be a finite number")
    number = float(value)
    if not np.isfinite(number) or not 0.1 <= number <= 20.0:
        raise ValueError("Retracement deviation must be between 0.1 and 20.0")
    return number


def validate_indicator_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an indicator settings payload.

    The returned structure contains only supported fields, making it safe to
    persist in an account configuration or pass to the chart calculator.
    """
    if not isinstance(settings, Mapping):
        raise ValueError("Indicator settings must be an object")

    moving_averages = settings.get("moving_averages")
    if not isinstance(moving_averages, Sequence) or isinstance(moving_averages, (str, bytes)):
        raise ValueError("Moving averages must contain six entries")
    if len(moving_averages) != len(MA_DEFAULT_PERIODS):
        raise ValueError("Exactly six moving averages are required")

    normalized_mas = []
    for index, item in enumerate(moving_averages, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"Moving average {index} is invalid")
        ma_type = str(item.get("type", "")).upper()
        if ma_type not in {"SMA", "EMA"}:
            raise ValueError(f"Moving average {index} type must be SMA or EMA")
        normalized_mas.append({
            "period": _positive_int(item.get("period"), f"Moving average {index} period"),
            "type": ma_type,
        })

    macd = settings.get("macd")
    if not isinstance(macd, Mapping):
        raise ValueError("MACD settings are invalid")
    fast = _positive_int(macd.get("fast"), "MACD fast period")
    slow = _positive_int(macd.get("slow"), "MACD slow period")
    signal = _positive_int(macd.get("signal"), "MACD signal period")
    if fast >= slow:
        raise ValueError("MACD fast period must be smaller than slow period")

    kdj = settings.get("kdj")
    if not isinstance(kdj, Mapping):
        raise ValueError("KDJ settings are invalid")
    kdj_period = _positive_int(kdj.get("period"), "KDJ RSV period")
    k_smoothing = _positive_int(kdj.get("k_smoothing"), "KDJ K smoothing")
    d_smoothing = _positive_int(kdj.get("d_smoothing"), "KDJ D smoothing")

    rsi = settings.get("rsi")
    if not isinstance(rsi, Mapping):
        raise ValueError("RSI settings are invalid")
    rsi_period = _positive_int(rsi.get("period"), "RSI period")

    fibonacci = settings.get("fibonacci")
    if not isinstance(fibonacci, Mapping):
        raise ValueError("Fibonacci settings are invalid")
    retracement = fibonacci.get("retracement")
    extension = fibonacci.get("extension")
    if not isinstance(retracement, Mapping) or not isinstance(extension, Mapping):
        raise ValueError("Fibonacci settings are invalid")
    retracement_enabled = retracement.get("enabled")
    extension_enabled = extension.get("enabled")
    if not isinstance(retracement_enabled, bool) or not isinstance(extension_enabled, bool):
        raise ValueError("Fibonacci enabled values must be boolean")

    return {
        "moving_averages": normalized_mas,
        "macd": {"fast": fast, "slow": slow, "signal": signal},
        "kdj": {
            "period": kdj_period,
            "k_smoothing": k_smoothing,
            "d_smoothing": d_smoothing,
        },
        "rsi": {"period": rsi_period},
        "fibonacci": {
            "retracement": {
                "enabled": retracement_enabled,
                "deviation": _bounded_deviation(retracement.get("deviation")),
                "depth": _bounded_depth(retracement.get("depth"), "Retracement depth"),
            },
            "extension": {
                "enabled": extension_enabled,
                "depth": _bounded_depth(extension.get("depth"), "Extension depth"),
            },
        },
    }


def normalize_indicator_settings(settings: Any) -> dict[str, Any]:
    """Merge legacy settings with defaults, then validate the complete result."""
    if not isinstance(settings, Mapping):
        return default_indicator_settings()
    merged = default_indicator_settings()
    for key, value in settings.items():
        if key not in merged:
            continue
        if isinstance(merged[key], dict) and isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                if nested_key not in merged[key]:
                    continue
                if isinstance(merged[key][nested_key], dict) and isinstance(nested_value, Mapping):
                    merged[key][nested_key].update(nested_value)
                else:
                    merged[key][nested_key] = deepcopy(nested_value)
        else:
            merged[key] = deepcopy(value)
    try:
        return validate_indicator_settings(merged)
    except (TypeError, ValueError):
        return default_indicator_settings()


def _ohlcv_series(ohlc: Mapping[str, Sequence[Any]], key: str, length: int) -> pd.Series:
    values = ohlc.get(key)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or len(values) != length:
        raise ValueError(f"OHLC field {key} must match dates length")
    return pd.to_numeric(pd.Series(values, dtype="object"), errors="coerce")


def _session_keys(dates: Sequence[Any], interval: str) -> pd.Series:
    if str(interval).lower() not in INTRADAY_INTERVALS:
        return pd.Series("all", index=range(len(dates)))
    parsed = pd.to_datetime(pd.Series(dates), errors="coerce")
    # A malformed timestamp cannot be allowed to join another trading session.
    return parsed.dt.strftime("%Y-%m-%d").fillna(pd.Series(range(len(dates))).astype(str))


def calculate_configurable_indicators(
    dates: Sequence[Any],
    ohlc: Mapping[str, Sequence[Any]],
    settings: Mapping[str, Any],
    interval: str,
) -> dict[str, Any]:
    """Calculate chart indicators from local OHLCV data and validated settings."""
    normalized = validate_indicator_settings(settings)
    length = len(dates)
    if not length:
        raise ValueError("K-line data is empty")

    close = _ohlcv_series(ohlc, "close", length)
    high = _ohlcv_series(ohlc, "high", length)
    low = _ohlcv_series(ohlc, "low", length)
    volume = _ohlcv_series(ohlc, "volume", length)

    moving_averages = []
    for ma in normalized["moving_averages"]:
        if ma["type"] == "EMA":
            values = close.ewm(span=ma["period"], adjust=False).mean()
        else:
            values = close.rolling(window=ma["period"]).mean()
        moving_averages.append(values.tolist())

    macd_settings = normalized["macd"]
    macd = (
        close.ewm(span=macd_settings["fast"], adjust=False).mean()
        - close.ewm(span=macd_settings["slow"], adjust=False).mean()
    )
    signal = macd.ewm(span=macd_settings["signal"], adjust=False).mean()

    kdj_settings = normalized["kdj"]
    lowest = low.rolling(kdj_settings["period"]).min()
    highest = high.rolling(kdj_settings["period"]).max()
    denominator = (highest - lowest).replace(0, np.nan)
    rsv = ((close - lowest) / denominator * 100).fillna(50)
    k_value = rsv.ewm(com=kdj_settings["k_smoothing"] - 1, adjust=False).mean()
    d_value = k_value.ewm(com=kdj_settings["d_smoothing"] - 1, adjust=False).mean()

    rsi_period = normalized["rsi"]["period"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = -delta.where(delta < 0, 0).rolling(rsi_period).mean()
    rsi = 100 - (100 / (1 + gain / loss))

    typical_price = (high + low + close) / 3
    valid_volume = volume.gt(0) & typical_price.notna()
    vwap = pd.Series(np.nan, index=close.index, dtype="float64")
    sessions = _session_keys(dates, interval)
    for _session, indexes in sessions.groupby(sessions).groups.items():
        group_indexes = list(indexes)
        group_valid = valid_volume.loc[group_indexes]
        if not group_valid.any():
            continue
        valid_indexes = group_valid[group_valid].index
        cumulative_volume = volume.loc[valid_indexes].cumsum()
        vwap.loc[valid_indexes] = (typical_price.loc[valid_indexes] * volume.loc[valid_indexes]).cumsum() / cumulative_volume

    return {
        "moving_averages": moving_averages,
        "macd": macd.tolist(),
        "signal": signal.tolist(),
        "hist": (macd - signal).tolist(),
        "kdj_k": k_value.tolist(),
        "kdj_d": d_value.tolist(),
        "kdj_j": (3 * k_value - 2 * d_value).tolist(),
        "rsi": rsi.tolist(),
        "vwap": vwap.tolist(),
    }
