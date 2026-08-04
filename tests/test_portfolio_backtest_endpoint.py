from __future__ import annotations

import datetime

import numpy as np
import pandas as pd

import stock_watch_list_back_end as backend


def _price_frame(start):
    dates = pd.date_range(start, periods=80, freq="B")
    values = {}
    for ticker, base in (("AAA", 100), ("SPY", 200), ("QQQ", 300)):
        values[("Adj Close", ticker)] = np.linspace(base, base + 20, len(dates))
    return pd.DataFrame(values, index=dates)


def test_portfolio_dca_backtest_endpoint_downloads_requested_range_without_sqlite(monkeypatch):
    today = datetime.date.today()
    start = today - datetime.timedelta(days=70)
    requested = []

    def fake_download(tickers, **kwargs):
        requested.extend(tickers)
        assert kwargs["interval"] == "1d"
        assert kwargs["auto_adjust"] is False
        assert kwargs["start"] == (start - datetime.timedelta(days=14)).isoformat()
        assert kwargs["end"] == (today + datetime.timedelta(days=1)).isoformat()
        return _price_frame(start)

    monkeypatch.setattr(backend.yf, "download", fake_download)
    monkeypatch.setattr(backend, "get_prices_with_cache", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("SQLite cache must not be used")))
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as client:
        response = client.post("/api/portfolio_dca_backtest", json={
            "tickers": ["AAA"],
            "start_date": start.isoformat(),
            "end_date": today.isoformat(),
            "frequency": "weekly",
            "monthly_timing": "start",
        })

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert {"AAA", "SPY", "QQQ"}.issubset(requested)
    assert {curve["key"] for curve in payload["curves"]} == {"portfolio", "SPY", "QQQ"}


def test_portfolio_dca_backtest_endpoint_accepts_history_older_than_normal_cache(monkeypatch):
    requested_start = datetime.date(2019, 1, 2)

    def fake_download(tickers, **kwargs):
        assert kwargs["start"] == (requested_start - datetime.timedelta(days=14)).isoformat()
        return _price_frame(requested_start)

    monkeypatch.setattr(backend.yf, "download", fake_download)
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as client:
        response = client.post("/api/portfolio_dca_backtest", json={
            "tickers": ["AAA"],
            "start_date": requested_start.isoformat(),
            "end_date": "2019-03-01",
        })

    assert response.status_code == 200
    assert response.get_json()["success"] is True


def test_portfolio_dca_backtest_endpoint_rejects_future_end_date():
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as client:
        response = client.post("/api/portfolio_dca_backtest", json={
            "tickers": ["AAA"],
            "start_date": (tomorrow - datetime.timedelta(days=30)).isoformat(),
            "end_date": tomorrow.isoformat(),
        })

    assert response.status_code == 400
    assert response.get_json()["success"] is False
