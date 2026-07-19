import math

import pandas as pd
import pytest

from kline_indicators import (
    calculate_configurable_indicators,
    default_indicator_settings,
    normalize_indicator_settings,
    validate_indicator_settings,
)


def _ohlcv(values, volumes=None):
    volumes = volumes or [100] * len(values)
    return {
        "open": values,
        "high": [value + 2 for value in values],
        "low": [value - 1 for value in values],
        "close": values,
        "volume": volumes,
    }


def test_default_calculations_match_existing_indicator_formulas():
    values = list(range(100, 140))
    ohlc = _ohlcv(values)
    dates = pd.date_range("2026-01-01", periods=len(values), freq="D").astype(str).tolist()

    result = calculate_configurable_indicators(dates, ohlc, default_indicator_settings(), "1d")
    close = pd.Series(values, dtype="float64")

    assert result["moving_averages"][0][-1] == pytest.approx(close.rolling(5).mean().iloc[-1])
    expected_macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    assert result["macd"][-1] == pytest.approx(expected_macd.iloc[-1])
    expected_signal = expected_macd.ewm(span=9, adjust=False).mean()
    assert result["signal"][-1] == pytest.approx(expected_signal.iloc[-1])

    delta = close.diff()
    expected_rsi = 100 - 100 / (1 + delta.where(delta > 0, 0).rolling(14).mean() / -delta.where(delta < 0, 0).rolling(14).mean())
    assert result["rsi"][-1] == pytest.approx(expected_rsi.iloc[-1])


def test_custom_ema_and_indicator_parameters_are_applied():
    settings = default_indicator_settings()
    settings["moving_averages"][0] = {"period": 3, "type": "EMA"}
    settings["macd"] = {"fast": 3, "slow": 6, "signal": 2}
    settings["kdj"] = {"period": 3, "k_smoothing": 2, "d_smoothing": 2}
    settings["rsi"] = {"period": 3}
    values = [10, 12, 11, 14, 16, 15]
    result = calculate_configurable_indicators([f"2026-01-0{i + 1}" for i in range(6)], _ohlcv(values), settings, "1d")

    assert result["moving_averages"][0][-1] == pytest.approx(pd.Series(values).ewm(span=3, adjust=False).mean().iloc[-1])
    assert not math.isnan(result["kdj_k"][-1])
    assert not math.isnan(result["rsi"][-1])


def test_invalid_settings_are_rejected_and_bad_saved_values_reset_to_defaults():
    settings = default_indicator_settings()
    settings["macd"] = {"fast": 26, "slow": 12, "signal": 9}
    with pytest.raises(ValueError, match="fast period"):
        validate_indicator_settings(settings)

    assert normalize_indicator_settings({"moving_averages": []}) == default_indicator_settings()


def test_vwap_ignores_zero_volume_and_resets_for_intraday_sessions():
    dates = ["2026-01-02 09:30", "2026-01-02 16:00", "2026-01-02 18:00", "2026-01-03 09:30"]
    ohlc = {
        "open": [10, 20, 30, 40],
        "high": [12, 22, 32, 42],
        "low": [9, 19, 29, 39],
        "close": [11, 21, 31, 41],
        "volume": [100, 300, 0, 200],
    }
    result = calculate_configurable_indicators(dates, ohlc, default_indicator_settings(), "5m")

    assert result["vwap"][0] == pytest.approx((12 + 9 + 11) / 3)
    expected_second = (((12 + 9 + 11) / 3) * 100 + ((22 + 19 + 21) / 3) * 300) / 400
    assert result["vwap"][1] == pytest.approx(expected_second)
    assert math.isnan(result["vwap"][2])
    assert result["vwap"][3] == pytest.approx((42 + 39 + 41) / 3)


def test_non_intraday_vwap_is_cumulative_over_the_loaded_range():
    result = calculate_configurable_indicators(
        ["2026-01-02", "2026-01-03"],
        _ohlcv([10, 20], [100, 300]),
        default_indicator_settings(),
        "1d",
    )
    first = (12 + 9 + 10) / 3
    second = (22 + 19 + 20) / 3
    assert result["vwap"] == pytest.approx([first, (first * 100 + second * 300) / 400])
