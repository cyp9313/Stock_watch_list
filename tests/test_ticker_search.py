from ticker_search import search_candidates_from_quotes


def test_search_candidates_filter_invalid_yahoo_data_and_keep_multiple_tickers():
    candidates = search_candidates_from_quotes([
        {"symbol": "1810.HK", "longname": "Xiaomi\x00 Corporation", "exchDisp": "Hong Kong", "quoteType": "EQUITY"},
        {"symbol": "3CP.F", "shortname": "Xiaomi Frankfurt", "exchange": "FRA", "typeDisp": "Equity"},
        {"symbol": "1810.HK", "longname": "Duplicate"},
        {"symbol": "bad ticker<script>", "longname": "Invalid"},
    ])

    assert [item["ticker"] for item in candidates] == ["1810.HK", "3CP.F"]
    assert candidates[0]["name"] == "Xiaomi  Corporation"
