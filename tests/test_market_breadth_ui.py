from pathlib import Path


def test_market_breadth_watchlist_uses_only_compact_market_columns():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")
    assert 'BREADTH_WATCHLIST_COLUMNS = ("Ticker", "Price", "1D%", "5D%", "1M%")' in source
    breadth_start = source.index('with main_tabs[market_breadth_tab_index]:')
    breadth_end = source.index('with main_tabs[portfolio_tab_index]:', breadth_start)
    section = source[breadth_start:breadth_end]
    assert "columns=BREADTH_WATCHLIST_COLUMNS" in section
    assert "column_widths=" not in section
