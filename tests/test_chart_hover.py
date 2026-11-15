"""Regression tests for price-change values shown on Plotly hover cards."""

import plotly.graph_objects as go

from chart_hover import candlestick_hover_kwargs, close_change_labels
from nav_chart import build_nav_chart


def test_close_change_labels_use_previous_close_and_handle_missing_baselines():
    assert close_change_labels([100, 105, 99.75, None, 110, 0, 10, 10]) == [
        "N/A", "+5.00%", "-5.00%", "N/A", "N/A", "-100.00%", "N/A", "0.00%",
    ]


def test_candlestick_hover_preserves_ohlc_and_labels_non_daily_intervals():
    daily = go.Candlestick(
        x=["2026-11-12", "2026-11-13"], open=[100, 102], high=[105, 110],
        low=[99, 101], close=[101, 107],
        **candlestick_hover_kwargs([101, 107], "1d"),
    )
    assert list(daily.customdata) == ["N/A", "+5.94%"]
    assert "Open: %{open:.2f}" in daily.hovertemplate
    assert "Close: %{close:.2f}" in daily.hovertemplate
    assert "Daily change vs previous close" in daily.hovertemplate
    weekly = candlestick_hover_kwargs([100, 105], "1wk")
    assert "Change vs previous bar" in weekly["hovertemplate"]
    assert "Daily change" not in weekly["hovertemplate"]


def test_fund_nav_hover_shows_daily_change_without_fabricated_ohlc():
    fig = build_nav_chart({
        "ticker": "FUND:023313", "dates": ["2026-11-12", "2026-11-13"],
        "ohlc": {"close": [1.12, 1.14]},
    })
    nav_trace = fig.data[0]
    assert nav_trace.type == "scatter"
    assert list(nav_trace.customdata) == ["N/A", "+1.79%"]
    assert "Daily change vs previous NAV" in nav_trace.hovertemplate
    assert "Unit NAV: %{y:.4f}" in nav_trace.hovertemplate
