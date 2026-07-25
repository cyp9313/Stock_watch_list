from __future__ import annotations

from pathlib import Path
import pytest

from short_term_watchlist import (
    calculate_short_term_row,
    default_short_term_watchlist,
    normalize_short_term_watchlist,
    short_term_history_days,
    short_term_tickers,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _payload(*, latest_volume=100, start_hour=9, start_minute=30):
    count = 40
    closes = [100 + index * 0.1 for index in range(count)]
    dates = []
    hour, minute = start_hour, start_minute
    for _ in range(count):
        dates.append(f"2026-07-24 {hour:02d}:{minute:02d}")
        minute += 5
        hour += minute // 60
        minute %= 60
    return {
        "success": True,
        "dates": dates,
        "ohlc": {
            "open": closes,
            "high": [value + 0.2 for value in closes],
            "low": [value - 0.2 for value in closes],
            "close": closes,
            "volume": [100] * (count - 1) + [latest_volume],
        },
    }


def test_defaults_and_legacy_normalization_are_account_safe():
    defaults = default_short_term_watchlist()
    assert defaults["groups"] == {"Short-term": []}
    assert defaults["refresh"] == {"enabled": False, "interval_seconds": 10}
    assert defaults["settings"]["ma_1"] == {"period": 9, "type": "EMA"}

    config = normalize_short_term_watchlist({
        "groups": {"Momentum": ["AAPL", "aapl", "MSFT"]},
        "settings": {"ma_1": {"period": 5, "type": "SMA"}},
        "refresh": {"enabled": True, "interval_seconds": 20},
    })
    assert config["groups"] == {"Momentum": ["AAPL", "MSFT"]}
    assert config["settings"]["ma_1"] == {"period": 5, "type": "SMA"}
    assert config["settings"]["ma_2"] == defaults["settings"]["ma_2"]
    assert config["settings"]["rsi"] == {"period": 14}
    assert config["refresh"] == {"enabled": True, "interval_seconds": 20}
    assert short_term_tickers(config) == ["AAPL", "MSFT"]


def test_history_window_is_two_days_by_default_and_scales_for_long_periods():
    settings = default_short_term_watchlist()["settings"]
    assert short_term_history_days(settings) == 2
    long_settings = normalize_short_term_watchlist({
        "settings": {
            "ma_1": {"period": 500, "type": "EMA"},
            "ma_2": {"period": 21, "type": "EMA"},
            "macd": {"fast": 12, "slow": 26, "signal": 9},
            "bollinger": {"period": 20, "stddev": 2.0},
            "rsi": {"period": 14},
        },
    })["settings"]
    assert short_term_history_days(long_settings) == 20


def test_row_calculates_requested_metrics_and_inline_svg():
    config = default_short_term_watchlist()
    row = calculate_short_term_row("AAPL", _payload(), config["settings"])

    assert row["Price"] == pytest.approx(103.9)
    assert row["Bar Diff%"] == pytest.approx((103.9 - 103.8) / 103.8 * 100)
    assert row["MA Spread%"] > 0
    assert "#16a34a" in row["MA 1 / MA 2"]
    assert "#9333ea" in row["MA 1 / MA 2"]
    assert row["MACD Diff"] > 0
    assert row["Volume Ratio"] == pytest.approx(1.0)
    assert row["Diff BB Upper%"] < 0
    assert row["Diff VWAP%"] > 0
    assert row["RSI"] == pytest.approx(100.0)
    assert "<svg" in row["Candles (15)"]
    assert row["Candles (15)"].count("<rect") == 15
    assert "#2563eb" in row["MACD / Signal"]
    assert "#f59e0b" in row["MACD / Signal"]
    assert "stroke-dasharray='3 2'" in row["MACD / Signal"]
    assert "<rect" in row["Volume (15)"]
    assert "#0f766e" in row["VWAP / Close"]
    assert "#7c3aed" in row["RSI (30/70)"]
    assert row["RSI (30/70)"].count("stroke-dasharray='3 2'") == 2


def test_vwap_is_blank_for_zero_volume_or_after_hours_latest_bar():
    settings = default_short_term_watchlist()["settings"]
    assert calculate_short_term_row("AAPL", _payload(latest_volume=0), settings)["Diff VWAP%"] != calculate_short_term_row("AAPL", _payload(latest_volume=0), settings)["Diff VWAP%"]

    after_hours = _payload(start_hour=16, start_minute=0)
    assert calculate_short_term_row("AAPL", after_hours, settings)["Price Source"] == "After-hours"
    assert calculate_short_term_row("AAPL", after_hours, settings)["Diff VWAP%"] != calculate_short_term_row("AAPL", after_hours, settings)["Diff VWAP%"]


def test_crypto_prices_are_treated_as_regular_session_prices():
    row = calculate_short_term_row("BTC-USD", _payload(start_hour=16, start_minute=0), default_short_term_watchlist()["settings"])
    assert row["Price Source"] == "Regular"
    assert row["Diff VWAP%"] == pytest.approx(1.9127023050514984)


def test_invalid_or_short_payload_is_non_fatal():
    settings = default_short_term_watchlist()["settings"]
    assert calculate_short_term_row("AAPL", {"dates": [], "ohlc": {}}, settings)["Error"] == "Insufficient intraday data"
    assert normalize_short_term_watchlist({"settings": {"macd": {"fast": 30, "slow": 10, "signal": 9}}})["settings"] == default_short_term_watchlist()["settings"]


def test_multiuser_tab_has_its_own_kline_request_and_fragment_refresh():
    source = (REPO_ROOT / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    assert '"Short-term Watchlist"' in source
    assert 'main_tab_labels.insert(2, "Short-term Watchlist")' in source
    assert "if editable:" in source
    assert 'params={"ticker": ticker, "period": history_days, "interval": interval}' in source
    start = source.index("def render_short_term_watchlist")
    end = source.index("def build_breadth_chart", start)
    section = source[start:end]
    assert "@st.fragment(run_every=run_every)" in section
    assert 'run_every = 1 if short_config["refresh"]["enabled"] else None' in section
    assert "Refresh short-term data now" not in section
    assert "Auto-refresh stocks" in section
    assert "auto_refresh_stocks" not in section
    assert "rows_by_pair" in section
    assert "render_short_term_table(" in section
    assert "Search and add a security" in section
    assert section.index("Search and add a security") < section.index('with st.form(f"short_term_groups_form_')
    assert 'with st.expander("Indicator parameters", expanded=False):' in section
    assert "Volume bars = light blue" in section
    assert "Close = blue; VWAP = teal" in section
    assert '"Volume Ratio"' in source
    assert '"VWAP / Close"' in source
    assert '"1D%"' in source
    assert "short_term_reference_metrics(stock_data)" in section
    assert "ticker_background = beta_color" in source
    assert '"MACD Diff", "Diff VWAP%"' in source
    assert "short_term_history_days" in source
    assert "history_days=history_days" in section
