"""Regression checks for browser-side K-line legend persistence."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SOURCES = {
    name: (REPO_ROOT / name).read_text(encoding="utf-8")
    for name in ("app_streamlit.py", "app_streamlit_multiuser.py")
}


def test_kline_legend_state_has_a_storage_key_separate_from_zoom():
    for source in APP_SOURCES.values():
        assert 'f"stock_watchlist:kline_zoom:{storage_key}"' in source
        assert 'f"stock_watchlist:kline_legend:{storage_key}"' in source
        assert "const legendStorageKey = __LEGEND_STORAGE_KEY__;" in source


def test_kline_legend_state_is_saved_and_restored_after_rerender():
    for source in APP_SOURCES.values():
        assert 'chart.on("plotly_restyle", saveLegend);' in source
        assert "function saveLegend(eventData)" in source
        assert "function restoreLegend()" in source
        assert "restoreLegend();" in source
        assert "Plotly.restyle(chart, { visible: visibility }, indexes);" in source


def test_legend_preferences_use_stable_trace_keys():
    for source in APP_SOURCES.values():
        assert "function traceKeys(traces)" in source
        assert 'rawName.startsWith("Latest (") ? "Latest" : rawName' in source
