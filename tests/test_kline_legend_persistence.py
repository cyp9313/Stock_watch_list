"""Regression checks for browser-side K-line legend persistence."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (REPO_ROOT / "app_streamlit_multiuser.py").read_text(encoding="utf-8")


def test_kline_legend_state_has_a_storage_key_separate_from_zoom():
    assert 'f"stock_watchlist:kline_zoom:{storage_key}"' in APP_SOURCE
    assert 'f"stock_watchlist:kline_legend:{storage_key}"' in APP_SOURCE
    assert "const legendStorageKey = __LEGEND_STORAGE_KEY__;" in APP_SOURCE


def test_kline_legend_state_is_saved_and_restored_after_rerender():
    assert 'chart.on("plotly_restyle", saveLegend);' in APP_SOURCE
    assert "function saveLegend(eventData)" in APP_SOURCE
    assert "function restoreLegend()" in APP_SOURCE
    assert "restoreLegend();" in APP_SOURCE
    assert "Plotly.restyle(chart, { visible: visibility }, indexes);" in APP_SOURCE


def test_legend_preferences_use_stable_trace_keys():
    assert "function traceKeys(traces)" in APP_SOURCE
    assert 'rawName.startsWith("Latest (") ? "Latest" : rawName' in APP_SOURCE
