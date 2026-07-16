import stock_watch_list_back_end as backend


def test_beta_benchmark_defaults_to_sp500_for_us_tickers():
    assert backend.beta_benchmark_for_ticker("AAPL") == "^GSPC"
    assert backend.beta_benchmark_for_ticker("SPY") == "^GSPC"


def test_beta_benchmark_uses_sxr8_for_european_tickers():
    assert backend.beta_benchmark_for_ticker("SXR8.DE") == "SXR8.DE"
    assert backend.beta_benchmark_for_ticker("MC.PA") == "SXR8.DE"
    assert backend.beta_benchmark_for_ticker("ASML.AS") == "SXR8.DE"


def test_beta_benchmark_uses_shanghai_index_for_china_a_tickers():
    assert backend.beta_benchmark_for_ticker("600519.SS") == "000001.SS"
    assert backend.beta_benchmark_for_ticker("000001.SZ") == "000001.SS"


def test_beta_benchmarks_for_tickers_are_distinct_and_include_default():
    benchmarks = backend.beta_benchmarks_for_tickers(["AAPL", "SXR8.DE", "600519.SS", "MC.PA"])

    assert benchmarks == ["^GSPC", "SXR8.DE", "000001.SS"]
