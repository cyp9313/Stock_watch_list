"""Typed AkShare fallback must preserve real OHLCV and never invent NAV bars."""

from datetime import date
import json
import sqlite3
import sys
from types import SimpleNamespace

import pandas as pd

from akshare_fallback import eligible_for_fallback, fetch_daily, is_nav_symbol
from market_data_service import MarketDataService
from nav_chart import build_nav_chart


def _stock_frame():
    return pd.DataFrame({
        "日期": ["2026-11-12", "2026-11-13"], "开盘": [10.0, 11.0],
        "最高": [11.0, 12.0], "最低": [9.0, 10.0],
        "收盘": [10.5, 11.5], "成交量": [100, 110],
    })


def test_only_mainland_or_explicit_fund_eligible():
    assert eligible_for_fallback("600519.SS")
    assert eligible_for_fallback("159915.SZ")
    assert eligible_for_fallback("FUND:000001")
    assert not eligible_for_fallback("AAPL")
    assert not eligible_for_fallback("^GSPC")
    assert is_nav_symbol("fund:000001")


def test_current_akshare_stock_api_and_adjustment(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(
        stock_zh_a_hist_em=lambda **kw: (calls.append(kw), _stock_frame())[1],
    ))
    data = fetch_daily("600519.SS", start=date(2026, 11, 11), end=date(2026, 11, 14))
    assert calls[0] == dict(symbol="600519", period="daily", start_date="20261111", end_date="20261114", adjust="qfq")
    assert data["Close"].tolist() == [10.5, 11.5]
    assert data["Adj Close"].equals(data["Close"])
    assert data.attrs["price_basis"] == "qfq"


def test_etf_uses_traded_bars(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(fund_etf_hist_em=lambda **kw: _stock_frame()))
    data = fetch_daily("159915.SZ", start=date(2026, 11, 11), end=date(2026, 11, 14))
    assert data.attrs["instrument_type"] == "ETF"
    assert data["Volume"].tolist() == [100, 110]


def test_etf_eastmoney_rate_limit_uses_sina_raw_bars(monkeypatch):
    def limited(**kw):
        raise RuntimeError("push2 rate limit")
    raw = _stock_frame().rename(columns={
        "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "成交量": "volume",
    })
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(
        fund_etf_hist_em=limited,
        fund_etf_hist_sina=lambda **kw: raw if kw["symbol"] == "sz159915" else pd.DataFrame(),
    ))
    data = fetch_daily("159915.SZ", start=date(2026, 11, 11), end=date(2026, 11, 14))
    assert data.attrs["price_basis"] == "raw"
    assert data["Volume"].tolist() == [100, 110]


def test_fund_nav_has_no_fabricated_ohlcv(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(
        fund_open_fund_info_em=lambda **kw: pd.DataFrame({
            "净值日期": ["2026-11-12", "2026-11-13"], "单位净值": [1.12, 1.14],
        })
    ))
    data = fetch_daily("FUND:000001", start=date(2026, 11, 11), end=date(2026, 11, 14))
    assert data.attrs["instrument_type"] == "MUTUALFUND"
    assert set(data.columns) == {"Close", "Adj Close"}
    assert "Volume" not in data


def test_yfinance_fallback_only_when_daily_data_missing(monkeypatch):
    import market_data_service as service
    calls = []
    monkeypatch.setattr(service.yf, "download", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr(service, "fetch_daily", lambda ticker, period: (calls.append(ticker), _stock_frame())[1])
    MarketDataService.fetch_ohlcv("AAPL")
    assert calls == []
    MarketDataService.fetch_ohlcv("600519.SS")
    assert calls == ["600519.SS"]


def test_portfolio_report_close_batch_can_include_explicit_fund(monkeypatch):
    import market_data_service as service
    def fake_download(*a, **kw):
        assert kw["tickers"] == ["SPY"]
        return pd.DataFrame({("Adj Close", "SPY"): [100.0, 101.0]},
                            index=pd.to_datetime(["2026-11-12", "2026-11-13"]))
    nav = pd.DataFrame({"Adj Close": [1.12, 1.14]},
                       index=pd.to_datetime(["2026-11-12", "2026-11-13"]))
    monkeypatch.setattr(service.yf, "download", fake_download)
    monkeypatch.setattr(service, "fetch_daily", lambda *a, **kw: nav)
    close = MarketDataService.fetch_adjusted_close_batch(["FUND:000001", "SPY"])
    assert list(close.columns) == ["FUND:000001", "SPY"]
    assert close["FUND:000001"].iloc[-1] == 1.14


def test_nav_chart_is_not_candlestick():
    fig = build_nav_chart({"ticker": "FUND:000001", "dates": ["2026-11-12", "2026-11-13"],
                           "ohlc": {"close": [1.12, 1.14]}})
    assert fig is not None
    assert all(trace.type == "scatter" for trace in fig.data)


def test_nav_watchlist_sparkline_uses_only_published_closes():
    import stock_watch_list_back_end as backend
    svg = backend.nav_sparkline_svg([1.0, None, float("nan"), 1.1, 1.2])
    assert "<svg" in svg
    assert "width='96' height='36'" in svg
    assert "Last 3 published daily unit NAV values" in svg
    assert "<polyline" in svg and "<circle" in svg
    assert "<rect" not in svg and "<line" not in svg
    assert backend.nav_sparkline_svg([None, float("nan")]) == ""
    assert "<circle" in backend.nav_sparkline_svg([1.1])
    short_svg = backend.nav_sparkline_svg(range(20), width=80, height=30, limit=15)
    assert "width='80' height='30'" in short_svg
    assert "Last 15 published daily unit NAV values" in short_svg


def test_cache_provider_switch_replaces_old_price_basis(monkeypatch):
    import stock_watch_list_back_end as backend
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE price_cache (ticker TEXT, date TEXT, adj_close REAL, volume REAL, "
                 "open_price REAL, high_price REAL, low_price REAL, close_price REAL, "
                 "PRIMARY KEY(ticker, date))")
    conn.execute("CREATE TABLE price_cache_source (ticker TEXT PRIMARY KEY, provider TEXT, last_checked TEXT, price_basis TEXT)")
    conn.execute("INSERT INTO price_cache VALUES ('600519.SS', '2026-11-11', 999, 1, 1, 1, 1, 1)")
    fallback = pd.DataFrame({"Open": [10], "High": [11], "Low": [9], "Close": [10.5],
                             "Adj Close": [10.5], "Volume": [100]}, index=pd.to_datetime(["2026-11-12"]))
    monkeypatch.setattr(backend, "fetch_akshare_daily", lambda *a, **kw: fallback)
    backend._cached_fallback_for_missing(conn, ["600519.SS"], pd.DataFrame(), "2d")
    assert conn.execute("SELECT date, adj_close FROM price_cache").fetchall() == [("2026-11-12", 10.5)]
    assert conn.execute("SELECT provider FROM price_cache_source").fetchone()[0] == "akshare"
    conn.close()


def test_failed_cache_replacement_keeps_old_prices(monkeypatch):
    import stock_watch_list_back_end as backend
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE price_cache (ticker TEXT, date TEXT, adj_close REAL, volume REAL, "
                 "open_price REAL, high_price REAL, low_price REAL, close_price REAL, "
                 "PRIMARY KEY(ticker, date))")
    conn.execute("CREATE TABLE price_cache_source (ticker TEXT PRIMARY KEY, provider TEXT, last_checked TEXT, price_basis TEXT)")
    conn.execute("INSERT INTO price_cache VALUES ('600519.SS', '2026-11-11', 999, 1, 1, 1, 1, 1)")
    monkeypatch.setattr(backend, "fetch_akshare_daily", lambda *a, **kw: _stock_frame())
    def fail_write(*a, **kw):
        raise sqlite3.OperationalError("simulated write failure")
    monkeypatch.setattr(backend, "_save_to_price_cache", fail_write)
    backend._cached_fallback_for_missing(conn, ["600519.SS"], pd.DataFrame(), "2d")
    assert conn.execute("SELECT date, adj_close FROM price_cache").fetchall() == [("2026-11-11", 999.0)]
    conn.close()


def test_nav_report_does_not_claim_equity_scoring(monkeypatch):
    import daily_report.fund_nav_report as nav_report
    index = pd.date_range("2026-09-01", periods=80, freq="B")
    frame = pd.DataFrame({"Close": [1 + i / 1000 for i in range(80)]}, index=index)
    monkeypatch.setattr(nav_report, "fetch_daily", lambda *a, **kw: frame)
    result = nav_report.generate_fund_nav_report("FUND:000001", include_telegram_summary=True)
    assert result["success"]
    assert result["decision_dashboard"] is False
    assert b"html" in result["html_bytes"]
    assert "MA20" in result["html_bytes"].decode()
    assert result["telegram_messages"]


def test_fund_price_cache_is_close_only_and_reused_same_day(tmp_path, monkeypatch):
    import stock_watch_list_back_end as backend
    path = str(tmp_path / "prices.db")
    backend.init_database(path)
    monkeypatch.setattr(backend, "get_db_connection", lambda: backend.sqlite3.connect(path))
    monkeypatch.setattr(backend.yf, "download", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fund must not hit yfinance")))
    calls = []
    frame = pd.DataFrame({"Close": [1.1234, 1.2345], "Adj Close": [1.1234, 1.2345]},
                         index=pd.to_datetime(["2026-11-12", "2026-11-13"]))
    monkeypatch.setattr(backend, "fetch_akshare_daily", lambda *a, **kw: (calls.append(1), frame)[1])
    first = backend.get_prices_with_cache(["FUND:000001"])
    second = backend.get_prices_with_cache(["FUND:000001"])
    assert len(calls) == 1
    assert first[("Adj Close", "FUND:000001")].dropna().tolist() == [1.1234, 1.2345]
    assert second[("Open", "FUND:000001")].isna().all()


def test_fund_kline_endpoint_returns_nav_without_fake_ohlcv(monkeypatch):
    import stock_watch_list_back_end as backend
    frame = pd.DataFrame({"Close": [1.1234, 1.2345]},
                         index=pd.to_datetime(["2026-11-12", "2026-11-13"]))
    monkeypatch.setattr(backend, "fetch_akshare_daily", lambda *a, **kw: frame)
    monkeypatch.setattr(backend, "set_request_cache_db", lambda: backend.CURRENT_DB_PATH.set(None))
    with backend.app.test_client() as client:
        response = client.get("/api/kline_data?ticker=FUND:000001&period=365&interval=1d")
        payload = response.get_json()
        assert payload["success"] is True
        assert payload["instrument_type"] == "MUTUALFUND"
        assert payload["ohlc"] == {"close": [1.1234, 1.2345]}
        intraday = client.get("/api/kline_data?ticker=FUND:000001&interval=5m")
        assert intraday.get_json()["success"] is False


def test_mainland_kline_fallback_is_strict_json_with_real_candles(monkeypatch):
    import stock_watch_list_back_end as backend
    class EmptyTicker:
        info = {}
        def history(self, **kw):
            return pd.DataFrame()
    dates = pd.date_range("2026-09-01", periods=35, freq="B")
    close = pd.Series([10 + i / 10 for i in range(len(dates))], index=dates)
    frame = pd.DataFrame({"Open": close - 0.1, "High": close + 0.2,
                          "Low": close - 0.2, "Close": close, "Volume": 1000}, index=dates)
    frame.attrs.update(instrument_type="EQUITY", price_basis="qfq")
    monkeypatch.setattr(backend.yf, "Ticker", lambda ticker: EmptyTicker())
    monkeypatch.setattr(backend, "fetch_akshare_daily", lambda *a, **kw: frame)
    monkeypatch.setattr(backend, "get_cached_stock_analysis", lambda *a, **kw: {})
    monkeypatch.setattr(backend, "set_request_cache_db", lambda: backend.CURRENT_DB_PATH.set(None))
    with backend.app.test_client() as client:
        response = client.get("/api/kline_data?ticker=600519.SS&period=365&interval=1d")
    payload = json.loads(response.data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    assert payload["success"] is True
    assert payload["price_basis"] == "qfq"
    assert len(payload["ohlc"]["open"]) == 35
    assert payload["indicators"]["chip_prices"] == []


def test_report_service_routes_explicit_fund_without_stock_agent(monkeypatch):
    import daily_report.fund_nav_report as nav_report
    import daily_report.service as service
    monkeypatch.setattr(nav_report, "generate_fund_nav_report", lambda ticker, **kw: {"success": True, "ticker": ticker})
    assert service.generate_report("fund:000001")["ticker"] == "FUND:000001"
    assert service.generate_report("OTHER:000001")["success"] is False


def test_trading212_completed_history_can_fallback_without_today(monkeypatch):
    import trading212_monitor as monitor
    class EmptyTicker:
        def history(self, **kw):
            return pd.DataFrame()
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=lambda ticker: EmptyTicker()))
    frame = pd.DataFrame({"Close": [10.0, 11.0], "Volume": [100.0, 110.0]},
                         index=pd.to_datetime(["2026-11-12", "2026-11-13"]))
    monkeypatch.setattr(monitor, "fetch_akshare_daily", lambda *a, **kw: frame)
    rows = monitor._download_yfinance_daily_history("600519.SS", date(2026, 11, 13))
    assert rows == [("2026-11-12", 10.0, 100.0)]
