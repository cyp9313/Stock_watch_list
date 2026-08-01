from pathlib import Path


def test_market_breadth_watchlist_uses_only_compact_market_columns():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    assert 'BREADTH_WATCHLIST_COLUMNS = ("Ticker", "Price", "1D%", "5D%", "1M%")' in source
    breadth_start = source.index('def render_market_breadth_panel')
    breadth_end = source.index('\n_backend_ok', breadth_start)
    section = source[breadth_start:breadth_end]
    assert "columns=BREADTH_WATCHLIST_COLUMNS" in section
    assert "column_widths=" not in section


def test_market_breadth_page_has_account_scoped_screener_tabs():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    breadth_start = source.index('with main_tabs[market_breadth_tab_index]:')
    breadth_end = source.index('with main_tabs[portfolio_tab_index]:', breadth_start)
    section = source[breadth_start:breadth_end]
    assert '"Stock Screener", "Screening History"' in section
    assert "if editable:" in section
