from __future__ import annotations

from pathlib import Path
import pytest

from short_term_watchlist import (
    calculate_short_term_row,
    consume_macd_alert_events,
    default_short_term_watchlist,
    macd_alert_event,
    normalize_short_term_watchlist,
    short_term_history_days,
    short_term_alert_events,
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
    assert defaults["alerts"] == {
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
            "vwap_bands": {"enabled": False, "threshold": 5.0},
            "rsi": {"enabled": False, "threshold": 2.0},
        },
        "ticker_enabled": {},
    }
    assert defaults["settings"]["ma_1"] == {"period": 9, "type": "EMA"}
    assert defaults["settings"]["atr"] == {"period": 14}
    assert defaults["settings"]["adx"] == {"period": 14}

    config = normalize_short_term_watchlist({
        "groups": {"Momentum": ["AAPL", "aapl", "MSFT"]},
        "settings": {"ma_1": {"period": 5, "type": "SMA"}},
        "refresh": {"enabled": True, "interval_seconds": 20},
    })
    assert config["groups"] == {"Momentum": ["AAPL", "MSFT"]}
    assert config["settings"]["ma_1"] == {"period": 5, "type": "SMA"}
    assert config["settings"]["ma_2"] == defaults["settings"]["ma_2"]
    assert config["settings"]["rsi"] == {"period": 14}
    assert config["settings"]["atr"] == {"period": 14}
    assert config["settings"]["adx"] == {"period": 14}
    assert config["refresh"] == {"enabled": True, "interval_seconds": 20}
    assert config["alerts"] == defaults["alerts"]
    assert short_term_tickers(config) == ["AAPL", "MSFT"]


def test_alert_normalization_keeps_only_valid_account_safe_values():
    config = normalize_short_term_watchlist({
        "alerts": {
            "enabled": True,
            "intervals": {"5m": False, "15m": True},
            "near_enabled": False,
            "confirmed_enabled": True,
            "duration_seconds": 30,
            "signals": {"ema": {"enabled": True, "threshold": 3.0}},
            "ticker_enabled": {"AAPL": False},
        },
    })
    assert config["alerts"] == {
        "enabled": True,
        "intervals": {"5m": False, "15m": True},
        "near_enabled": False,
        "confirmed_enabled": True,
        "duration_seconds": 30,
        "signals": {
            "macd": {"enabled": True, "threshold": 5.0},
            "ema": {"enabled": True, "threshold": 3.0},
            "bollinger": {"enabled": False, "threshold": 10.0},
            "vwap": {"enabled": False, "threshold": 5.0},
            "vwap_bands": {"enabled": False, "threshold": 5.0},
            "rsi": {"enabled": False, "threshold": 2.0},
        },
        "ticker_enabled": {"AAPL": False},
    }
    assert normalize_short_term_watchlist({"alerts": {"signals": {"macd": {"threshold": 9999}}}})["alerts"]["signals"]["macd"]["threshold"] == 5.0
    assert normalize_short_term_watchlist({"alerts": {"duration_seconds": 7}})["alerts"]["duration_seconds"] == 15


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
            "atr": {"period": 14},
        },
    })["settings"]
    assert short_term_history_days(long_settings) == 20


def test_row_calculates_requested_metrics_and_inline_svg():
    config = default_short_term_watchlist()
    row = calculate_short_term_row("AAPL", _payload(), config["settings"])

    assert row["Price"] == pytest.approx(103.9)
    assert row["Price Timestamp"] == "2026-07-24 12:45 EDT"
    assert row["Bar Diff%"] == pytest.approx((103.9 - 103.8) / 103.8 * 100)
    assert row["MA Spread‱"] > 0
    assert "#16a34a" in row["MA 1 / MA 2"]
    assert "#9333ea" in row["MA 1 / MA 2"]
    assert row["MACD Diff‱"] > 0
    assert row["MACD Diff Previous‱"] > 0
    assert row["Alert Bar Timestamp"] == "2026-07-24 12:45"
    assert row["Volume Ratio"] == pytest.approx(1.0)
    assert row["Diff BB Upper%"] < 0
    assert row["BB Upper Cross (%)"] == pytest.approx(row["Diff BB Upper%"])
    assert row["BB / Close"].count("stroke-dasharray='3 2'") == 2
    assert row["Diff VWAP%"] > 0
    assert row["RSI"] == pytest.approx(100.0)
    assert "<svg" in row["Candles (15)"]
    assert row["Candles (15)"].count("<rect") == 15
    assert "#2563eb" in row["MACD / Signal"]
    assert "#f59e0b" in row["MACD / Signal"]
    assert "stroke-dasharray='3 2'" in row["MACD / Signal"]
    assert "<rect" in row["Volume (15)"]
    assert "#0f766e" in row["VWAP / Close"]
    assert row["VWAP / Close"].count("stroke-dasharray='3 2'") == 2
    assert row["VWAP Lower Cross (%)"] == pytest.approx(row["VWAP Upper Cross (%)"] + 100.0)
    assert "#7c3aed" in row["RSI (30/70)"]
    assert row["RSI (30/70)"].count("stroke-dasharray='3 2'") == 2
    assert row["ATR"] > 0
    assert "#1d4ed8" in row["ATR (15)"]
    assert row["ADX"] > 25
    assert "#059669" in row["ADX (15)"]
    assert "M 3.0,17.4 L 77.0,17.4" in row["ADX (15)"]
    assert "M 3.0,15.0 L 77.0,15.0" in row["ADX (15)"]


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


def test_macd_alert_events_cover_near_confirmed_and_invalid_rows():
    alerts = {**default_short_term_watchlist()["alerts"], "enabled": True}
    near_bullish = {
        "Ticker": "AAPL", "MACD Diff‱": -2.0,
        "MACD Diff Previous‱": -5.0, "Alert Bar Timestamp": "2026-07-24 10:00",
    }
    event = macd_alert_event(near_bullish, "5m", alerts)
    assert event and event["type"] == "bullish_near"

    confirmed_bearish = {
        "Ticker": "AAPL", "MACD Diff‱": -1.0,
        "MACD Diff Previous‱": 2.0, "Alert Bar Timestamp": "2026-07-24 10:05",
    }
    event = macd_alert_event(confirmed_bearish, "15m", alerts)
    assert event and event["type"] == "bearish_confirmed"

    far_from_cross = {**near_bullish, "MACD Diff‱": -20.0}
    assert macd_alert_event(far_from_cross, "5m", alerts) is None
    assert macd_alert_event({"Ticker": "AAPL"}, "5m", alerts) is None
    assert macd_alert_event(near_bullish, "5m", default_short_term_watchlist()["alerts"]) is None
    assert macd_alert_event(near_bullish, "5m", {**alerts, "intervals": {"5m": False, "15m": True}}) is None


def test_other_short_term_alert_signals_and_ticker_switches_are_independent():
    alerts = default_short_term_watchlist()["alerts"]
    alerts = {
        **alerts,
        "enabled": True,
        "signals": {
            **alerts["signals"],
            "macd": {"enabled": False, "threshold": 5.0},
            "ema": {"enabled": True, "threshold": 5.0},
            "bollinger": {"enabled": True, "threshold": 10.0},
            "vwap": {"enabled": True, "threshold": 5.0},
            "rsi": {"enabled": True, "threshold": 2.0},
        },
    }
    row = {
        "Ticker": "AAPL", "Alert Bar Timestamp": "2026-07-24 10:00",
        "MA Cross (bp)": 1.0, "MA Cross Previous (bp)": -1.0,
        "BB Upper Cross (%)": 11.0, "BB Upper Cross Previous (%)": -2.0,
        "BB Lower Cross (%)": -11.0, "BB Lower Cross Previous (%)": 2.0,
        "VWAP Cross (bp)": 1.0, "VWAP Cross Previous (bp)": -1.0,
        "RSI 30 Cross": 1.0, "RSI 30 Cross Previous": -1.0,
        "RSI 70 Cross": -1.0, "RSI 70 Cross Previous": 1.0,
    }
    signals = {event["signal"] for event in short_term_alert_events(row, "5m", alerts)}
    assert signals == {"ema", "bollinger_upper", "bollinger_lower", "vwap", "rsi", "rsi_upper"}
    assert short_term_alert_events(row, "5m", {**alerts, "ticker_enabled": {"AAPL": False}}) == []


def test_bollinger_near_threshold_uses_percent_of_band_width():
    alerts = default_short_term_watchlist()["alerts"]
    alerts = {
        **alerts,
        "enabled": True,
        "confirmed_enabled": False,
        "signals": {
            **alerts["signals"],
            "bollinger": {"enabled": True, "threshold": 10.0},
        },
    }
    row = {
        "Ticker": "AAPL", "Alert Bar Timestamp": "2026-07-24 10:00",
        "BB Upper Cross (%)": -9.0, "BB Upper Cross Previous (%)": -20.0,
        "BB Lower Cross (%)": 40.0, "BB Lower Cross Previous (%)": 35.0,
    }
    events = short_term_alert_events(row, "5m", alerts)
    assert [(event["signal"], event["type"]) for event in events] == [("bollinger_upper", "bullish_near")]

    far_row = {**row, "BB Upper Cross (%)": -11.0}
    assert short_term_alert_events(far_row, "5m", alerts) == []


def test_vwap_band_alerts_are_independently_configured_and_deduplicated():
    alerts = default_short_term_watchlist()["alerts"]
    alerts = {
        **alerts,
        "enabled": True,
        "signals": {
            **alerts["signals"],
            "vwap_bands": {"enabled": True, "threshold": 5.0},
        },
    }
    row = {
        "Ticker": "AAPL", "Alert Bar Timestamp": "2026-07-24 10:00",
        "VWAP Upper Cross (%)": 1.0, "VWAP Upper Cross Previous (%)": -1.0,
        "VWAP Lower Cross (%)": -2.0, "VWAP Lower Cross Previous (%)": -6.0,
    }
    events = short_term_alert_events(row, "5m", alerts)
    assert [(event["signal"], event["type"]) for event in events] == [
        ("vwap_upper", "bullish_confirmed"),
        ("vwap_lower", "bullish_near"),
    ]
    assert short_term_alert_events(row, "5m", {**alerts, "signals": {**alerts["signals"], "vwap_bands": {"enabled": False, "threshold": 5.0}}}) == []


def test_macd_alert_consumption_bootstraps_and_deduplicates_per_bar():
    alerts = {**default_short_term_watchlist()["alerts"], "enabled": True}
    initial_rows = {
        ("AAPL", "5m"): {
            "Ticker": "AAPL", "MACD Diff‱": -2.0,
            "MACD Diff Previous‱": -5.0, "Alert Bar Timestamp": "2026-07-24 10:00",
        },
    }
    events, state = consume_macd_alert_events(
        initial_rows, alerts, None, monitoring_enabled=True, signal_signature="settings-v1",
    )
    assert events == []  # Loading a page must not replay an already-present signal.

    events, state = consume_macd_alert_events(
        initial_rows, alerts, state, monitoring_enabled=True, signal_signature="settings-v1",
    )
    assert events == []

    refreshed_rows = {
        ("AAPL", "5m"): {
            **initial_rows[("AAPL", "5m")],
            "MACD Diff‱": 1.0,
            "MACD Diff Previous‱": -2.0,
            "Alert Bar Timestamp": "2026-07-24 10:05",
        },
    }
    events, state = consume_macd_alert_events(
        refreshed_rows, alerts, state, monitoring_enabled=True, signal_signature="settings-v1",
    )
    assert [event["type"] for event in events] == ["bullish_confirmed"]

    events, _ = consume_macd_alert_events(
        refreshed_rows, alerts, state, monitoring_enabled=True, signal_signature="settings-v1",
    )
    assert events == []

    events, state = consume_macd_alert_events(
        refreshed_rows, alerts, state, monitoring_enabled=False, signal_signature="settings-v2",
    )
    assert events == []


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
    assert '"BB / Close"' in source
    assert '"VWAP / Close"' in source
    assert '"1D%"' in source
    assert '["Ticker", "Name", "Price", "Candles (20)", "ADX", "1D%"' in source
    assert 'cell_content = val if col == "Candles (20)" else html.escape(val)' in source
    assert 'elif col in {"Name", "Candles (20)"}:' in source
    assert '"Candles (20)": 104' in source
    assert "min-width:100%" not in source
    assert "short_term_reference_metrics(stock_data)" in section
    assert "ticker_background = beta_color" in source
    assert source.count('if col == "Ticker":\n                tooltip_text = ticker_name') == 2
    assert source.count('price_timestamp_tooltip(') >= 4
    assert '"Name": str(name).strip() if pd.notna(name) else ""' in source
    assert 'ticker_tooltip = " — ".join(part for part in (ticker_name, ticker_error) if part)' in source
    assert '"MACD Diff‱"' in source
    assert 'short_term_diverging_color(value, clip=15.0)' in source
    assert '"macd": {"MACD / Signal"}' in source
    assert '"bollinger_lower": {"BB / Close"}' in source
    assert '"rsi_upper": {"RSI (30/70)"}' in source
    assert '"% of VWAP band width"' in source
    assert '"ATR (15)"' in source
    assert 'st.number_input("ATR period"' in section
    assert '"ADX (15)"' in source
    assert 'st.number_input("ADX period"' in section
    assert "adx_color(value)" in source
    assert 'return f"{float(value):.2f}" if pd.notna(value) else ""' in source
    assert "short_term_history_days" in source
    assert "history_days=history_days" in section
    assert "Short-term audio alerts" in section
    assert 'with st.form(f"short_term_alert_form_' in section
    assert 'st.form_submit_button("Apply MACD alert settings"' in section
    assert 'div[class*="st-key-short_term_alert_"]:has(input:checked)' in source
    assert '"Alert sound and table highlight duration"' in section
    assert 'alert_pairs=active_alerts' in section
    assert "short-term-macd-alert-cell" in source
    assert "white-space:normal; overflow-wrap:anywhere; line-height:1.12;" in source
    assert source.count("<th title='{html.escape(") >= 3
    plot_start = source.index('plot = st.button("Plot"')
    plot_end = source.index("auto_refresh_enabled =", plot_start)
    plot_section = source[plot_start:plot_end]
    assert 'with st.spinner(f"Refreshing {ticker} K-line data..."):' in plot_section
    assert "fetch_kline_data.clear()" in plot_section
    assert "render_macd_audio_alert(" in section
    assert "consume_macd_alert_events(" in section
    assert "force_refresh and short_config[\"refresh\"][\"enabled\"]" in section
    assert (REPO_ROOT / "macd_audio_alert_component" / "index.html").exists()
    component_source = (REPO_ROOT / "macd_audio_alert_component" / "index.html").read_text(encoding="utf-8")
    assert "streamlit:componentReady" in component_source
    assert "AudioContext" in component_source
    assert "playRepeatingAlert" in component_source
    assert source.count('with display_col2:\n                show_ema_columns = st.toggle(') == 2
    assert source.count('with display_col3:\n                show_relative_momentum_columns = st.toggle(') == 2
