import math
from pathlib import Path

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
    true_range = pd.concat([close + 2 - (close - 1), (close + 2 - close.shift(1)).abs(), (close - 1 - close.shift(1)).abs()], axis=1).max(axis=1)
    assert result["atr"][-1] == pytest.approx(true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1])


def test_custom_ema_and_indicator_parameters_are_applied():
    settings = default_indicator_settings()
    settings["moving_averages"][0] = {"period": 3, "type": "EMA"}
    settings["macd"] = {"fast": 3, "slow": 6, "signal": 2}
    settings["kdj"] = {"period": 3, "k_smoothing": 2, "d_smoothing": 2}
    settings["rsi"] = {"period": 3}
    settings["atr"] = {"period": 3}
    values = [10, 12, 11, 14, 16, 15]
    result = calculate_configurable_indicators([f"2026-01-0{i + 1}" for i in range(6)], _ohlcv(values), settings, "1d")

    assert result["moving_averages"][0][-1] == pytest.approx(pd.Series(values).ewm(span=3, adjust=False).mean().iloc[-1])
    assert not math.isnan(result["kdj_k"][-1])
    assert not math.isnan(result["rsi"][-1])
    assert not math.isnan(result["atr"][-1])


def test_invalid_settings_are_rejected_and_bad_saved_values_reset_to_defaults():
    settings = default_indicator_settings()
    settings["macd"] = {"fast": 26, "slow": 12, "signal": 9}
    with pytest.raises(ValueError, match="fast period"):
        validate_indicator_settings(settings)

    assert normalize_indicator_settings({"moving_averages": []}) == default_indicator_settings()


def test_fibonacci_defaults_validation_and_legacy_settings_merge():
    defaults = default_indicator_settings()
    assert defaults["fibonacci"] == {
        "retracement": {"enabled": False, "deviation": 3.0, "depth": 10},
        "extension": {"enabled": False, "depth": 10},
    }
    assert defaults["atr"] == {"period": 14}

    legacy = {"moving_averages": [{"period": 7, "type": "EMA"}] + defaults["moving_averages"][1:]}
    merged = normalize_indicator_settings(legacy)
    assert merged["moving_averages"][0] == {"period": 7, "type": "EMA"}
    assert merged["fibonacci"] == defaults["fibonacci"]

    invalid = default_indicator_settings()
    invalid["fibonacci"]["retracement"]["deviation"] = True
    with pytest.raises(ValueError, match="deviation"):
        validate_indicator_settings(invalid)
    invalid = default_indicator_settings()
    invalid["fibonacci"]["extension"]["depth"] = 1
    with pytest.raises(ValueError, match="Extension depth"):
        validate_indicator_settings(invalid)


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


def test_multiuser_kline_loads_option_walls_only_on_manual_plot():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    plot_start = source.index("    if plot:\n", source.index("def render_kline"))
    auto_start = source.index("    auto_refresh_enabled =", plot_start)
    assert "fetch_options_open_interest(ticker, oi_horizon_months)" in source[plot_start:auto_start]
    body_start = source.index("    def _render_kline_body():", auto_start)
    body_end = source.index("\ndef render_report_form_fields", body_start)
    assert "fetch_options_open_interest(ticker, oi_horizon_months)" not in source[body_start:body_end]
    assert '"Open-Interest horizon (months)"' in source
    assert 'fig.add_vline(' in source
    assert 'latest_option_price = raw_closes[-1] if raw_closes else None' in source
    assert 'marker_color="#16a34a"' in source
    assert 'marker_color="#dc2626"' in source
    assert 'calculate_dealer_gex(nearest.get("gamma_legs", []), latest_price' in source
    assert 'calculate_dealer_gex(three_month.get("gamma_legs", []), latest_price' in source
    assert 'Gamma is recalculated from the cached option OI/IV whenever the K-line price refreshes.' in source
    assert 'name=f"ATR({indicator_settings[\'atr\'][\'period\']})"' in source
