import multiuser_store
import pandas as pd
import stock_watch_list_back_end as backend


def test_candidate_sources_use_indices_and_verified_user_equities(monkeypatch):
    monkeypatch.setattr(backend, "_cached_quote_types", lambda tickers: {ticker: {"AAPL": "EQUITY", "WNUC.DE": "EQUITY", "MYSTERY": "ETF"}.get(ticker) for ticker in tickers})
    sources, stats = backend.build_screening_candidate_sources(
        ["MSFT"], ["NVDA", "MSFT"],
        ["AAPL", "SPY", "BTC-USD", "EURUSD=X", "^VIX", "GC=F", "WNUC.DE", "MYSTERY"],
    )

    assert sources["MSFT"] == "S&P 500 · Nasdaq 100"
    assert sources["AAPL"] == "User watchlist"
    assert sources["WNUC.DE"] == "User watchlist"
    assert all(ticker not in sources for ticker in ("SPY", "BTC-USD", "EURUSD=X", "^VIX", "GC=F", "MYSTERY"))
    assert stats["excluded"]["已知 ETF"] == 1
    assert stats["excluded"]["加密货币"] == 1
    assert stats["excluded"]["ETF 非个股"] == 1


def test_screening_watchlist_tickers_includes_all_account_lists():
    config = multiuser_store.normalize_config({
        "stocks_pages": [{"name": "Stocks", "groups": {"G": ["AAPL"]}}],
        "broad_pages": [{"name": "Broad", "groups": {"G": ["^GSPC", "WNUC.DE"]}}],
        "portfolio_pages": [{"name": "Portfolio", "holdings": [{"ticker": "MSFT"}]}],
        "short_term_watchlist": {"groups": {"Short": ["NVDA"]}},
    })
    assert set(multiuser_store.screening_watchlist_tickers(config)) == {"AAPL", "^GSPC", "WNUC.DE", "MSFT", "NVDA"}


def test_breadth_endpoint_reuses_one_price_payload_for_screener(monkeypatch):
    captured = {}
    dates = pd.date_range("2025-01-01", periods=2, freq="B")
    breadth_frame = pd.DataFrame({"20MA_Ratio": [50.0, 60.0], "50MA_Ratio": [40.0, 50.0], "200MA_Ratio": [30.0, 40.0]}, index=dates)
    monkeypatch.setattr(backend, "build_screening_candidate_sources", lambda sp, ndx, user: ({"AAA": "S&P 500", "USER": "User watchlist"}, {"eligible_equities": 2, "excluded": {}}))
    def fake_prices(tickers, period):
        captured["tickers"] = tickers
        return pd.DataFrame({"placeholder": [1.0]})

    monkeypatch.setattr(backend, "get_prices_with_cache", fake_prices)
    monkeypatch.setattr(backend, "calculate_market_breadth", lambda *_: breadth_frame)
    monkeypatch.setattr(backend, "build_breadth_summary_rows", lambda *_: [])
    monkeypatch.setattr(backend, "build_breadth_chart_payload", lambda *_: {})
    monkeypatch.setattr(backend, "build_sp500_treemap_data", lambda *_: [])
    monkeypatch.setattr(backend, "build_nasdaq100_treemap_data", lambda *_: [])
    monkeypatch.setattr(backend, "get_cached_stock_analysis", lambda *args, **kwargs: {})
    monkeypatch.setattr(backend, "run_screener", lambda data, sources, fundamentals=None: {"version": "test", "strategies": [], "fundamental_coverage": {}})

    with backend.app.test_client() as client:
        response = client.post("/api/breadth_data", data={"sp500_symbols": '["AAA"]', "nasdaq100_symbols": '["BBB"]', "screening_candidates": '["USER"]', "enable_screener": "1"})
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["screener"]["universe"]["eligible_equities"] == 2
    assert set(captured["tickers"]) == {"AAA", "USER", "^GSPC", "^NDX"}


def test_screener_candidate_decoration_reuses_price_frame_for_beta(monkeypatch):
    dates = pd.date_range("2025-01-01", periods=5, freq="B")
    close = pd.DataFrame({"AAA": [100, 102, 101, 104, 106], "^GSPC": [100, 101, 100, 102, 103]}, index=dates)
    frame = pd.concat({"Adj Close": close}, axis=1)
    screener = {"strategies": [{"candidates": [{"Ticker": "AAA"}]}]}
    monkeypatch.setattr(backend, "get_sp500_constituents_metadata", lambda _: {"AAA": {"name": "Alpha Incorporated"}})
    monkeypatch.setattr(backend, "get_cached_ticker_names", lambda _: {})

    backend.decorate_screener_candidates(screener, frame, ["AAA"])

    row = screener["strategies"][0]["candidates"][0]
    assert row["Name"] == "Alpha Incorporated"
    assert isinstance(row["Beta"], float)
