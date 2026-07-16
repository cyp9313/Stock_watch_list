from ticker_mapping import (
    normalize_yfinance_ticker,
    stockanalysis_candidate_urls,
    stockanalysis_overview_url,
    stockanalysis_statistics_url,
    stockanalysis_symbol,
)


def test_ks_ticker_maps_to_stockanalysis_krx_quote_path():
    assert normalize_yfinance_ticker("5930.KS") == "005930.KS"
    assert normalize_yfinance_ticker("005930.KS") == "005930.KS"
    assert normalize_yfinance_ticker("krx:005930") == "005930.KS"

    assert stockanalysis_symbol("005930.KS") == ("krx", "005930")
    assert stockanalysis_overview_url("005930.KS") == "https://stockanalysis.com/quote/krx/005930/"
    assert (
        stockanalysis_statistics_url("005930.KS")
        == "https://stockanalysis.com/quote/krx/005930/statistics/"
    )
    assert stockanalysis_candidate_urls("005930.KS") == [
        "https://stockanalysis.com/quote/krx/005930/statistics/",
        "https://stockanalysis.com/quote/krx/005930/",
    ]


def test_kq_ticker_is_not_mapped_until_explicitly_supported():
    assert normalize_yfinance_ticker("005930.KQ") == "005930.KQ"
    assert stockanalysis_overview_url("005930.KQ") == "https://stockanalysis.com/stocks/005930.kq/"
