from pathlib import Path

import stock_watch_list_back_end


def test_daily_and_extended_price_timestamp_labels_are_unambiguous():
    assert stock_watch_list_back_end._price_timestamp_label("2026-07-27") == "Daily price date: 2026-07-27"
    assert (
        stock_watch_list_back_end._price_timestamp_label(
            "2026-07-27 16:30:00-04:00", "After-hours estimate"
        )
        == "After-hours estimate: 2026-07-27 16:30 EDT"
    )


def test_multiuser_watchlists_keep_long_tickers_visible_and_price_dates_hoverable():
    source = (Path(__file__).resolve().parents[1] / "app_streamlit_multiuser.py").read_text(encoding="utf-8")

    assert "def ticker_column_width(tickers):" in source
    assert 'white-space:normal; overflow-wrap:anywhere; word-break:break-word;' in source
    assert source.count("price_timestamp_tooltip(") >= 4
    assert source.count('if col in {"Ticker", "Name"}') == 2
    assert "def ticker_tooltip(name, beta, error=\"\"):" in source
    assert 'parts.append(f"Beta: {float(beta):.2f}")' in source
