from trading212_mapping_view import build_mapping_table_html


def test_mapping_table_html_contains_rows_and_escapes_broker_values():
    table = build_mapping_table_html([
        {
            "ticker": "AAPL_US_EQ",
            "display_name": "Apple <script>alert(1)</script>",
            "yfinance_ticker": "AAPL",
            "mapping_status": "mapped",
            "mapping_source": "trading212_ticker_candidate",
            "last_history_date": "2026-11-06",
            "last_error": None,
        }
    ])

    assert "Trading 212 ticker" in table
    assert "AAPL_US_EQ" in table
    assert "Apple &lt;script&gt;alert(1)&lt;/script&gt;" in table
    assert "<script>" not in table
    assert "max-height:420px" in table
