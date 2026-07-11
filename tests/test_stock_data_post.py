"""Tests for POST /api/stock_data migration (P1-6).

All Flask tests use a test client with mocked data-layer functions.
No real network, database, or yfinance calls are made.
"""
import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# ── Modules that other test files may stub with MagicMock ──
# We must restore the real versions before each test.
_REAL_MODULES = [
    "flask", "flask.testing", "flask.json", "werkzeug", "werkzeug.test",
    "pandas", "numpy", "pytz", "requests", "requests.sessions",
    "requests.adapters", "requests_cache",
    "fear_and_greed", "dotenv", "concurrent", "concurrent.futures",
    "stockanalysis_scraper", "ticker_mapping", "stock_watch_list_back_end",
]

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture(autouse=True)
def _ensure_real_modules():
    """Restore real flask/pandas/etc. before each test.

    Other test files (e.g. test_db_init_separation.py) stub flask as a
    MagicMock in sys.modules. This fixture undoes that so Flask's
    test_client works properly.
    """
    # Pop modules that are MagicMock stubs, or stock_watch_list_back_end
    # (which may have been imported against stubbed dependencies)
    for _m in _REAL_MODULES:
        mod = sys.modules.get(_m)
        if mod is None:
            continue
        if _m == "stock_watch_list_back_end" or isinstance(mod, MagicMock):
            sys.modules.pop(_m, None)

    # Also pop submodules of requests/requests_cache so that reimport
    # re-binds package-level attributes.  Without this, from .sessions
    # import Session in requests/__init__.py skips re-binding
    # requests.sessions because the cached submodule still exists.
    for _prefix in ("requests.", "requests_cache."):
        for _k in list(sys.modules):
            if _k.startswith(_prefix):
                sys.modules.pop(_k, None)

    # Reimport real modules
    import flask
    import pandas as pd
    import numpy as np
    import pytz
    import requests
    import warnings
    warnings.filterwarnings('ignore')
    import requests_cache
    requests_cache.uninstall_cache()
    from dotenv import load_dotenv
    import concurrent.futures

    # Stub only the modules that are truly unavailable or undesirable
    _yf_mock = MagicMock()

    def _mock_ticker_fn(ticker_symbol):
        t = MagicMock()
        t.calendar = {}
        t.get_earnings_dates = MagicMock(return_value=None)
        t.info = {}
        return t
    _yf_mock.Ticker = _mock_ticker_fn
    sys.modules["yfinance"] = _yf_mock

    _sa_scraper = MagicMock()
    _sa_scraper.scrape_batch = MagicMock(return_value={})
    _sa_scraper.should_query_forward_pe = MagicMock(return_value=True)
    sys.modules["stockanalysis_scraper"] = _sa_scraper

    _tm = MagicMock()
    _tm.normalize_yfinance_ticker = lambda t: t
    sys.modules["ticker_mapping"] = _tm

    sys.modules["fear_and_greed"] = MagicMock()

    _rc = MagicMock()
    _rc.uninstall_cache = MagicMock()
    sys.modules["requests_cache"] = _rc

    # Import backend fresh with real flask
    import stock_watch_list_back_end as backend

    # Make available to test functions via module globals
    globals()["backend"] = backend
    globals()["flask"] = flask
    globals()["pd"] = pd
    globals()["np"] = np

    yield


@pytest.fixture
def client():
    """Flask test client."""
    backend = sys.modules.get("stock_watch_list_back_end")
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as c:
        yield c


@pytest.fixture
def valid_payload():
    return {
        "groups": {"Tech": ["AAPL", "MSFT"]},
        "broad_market_tickers": ["^GSPC"],
    }


def _mock_prices():
    """Return a mock DataFrame with MultiIndex columns (as yfinance returns)."""
    import pandas as pd
    import numpy as np
    dates = pd.date_range("2024-01-01", periods=260, freq="B")
    arrays = [
        ["Adj Close"] * 2 + ["Volume"] * 2,
        ["AAPL", "MSFT"] * 2,
    ]
    data = {}
    data[("Adj Close", "AAPL")] = np.linspace(150, 180, 260)
    data[("Adj Close", "MSFT")] = np.linspace(300, 350, 260)
    data[("Volume", "AAPL")] = np.full(260, 1_000_000)
    data[("Volume", "MSFT")] = np.full(260, 2_000_000)
    return pd.DataFrame(data, index=dates)


def _common_mocks():
    """Return a context manager stack for common mocks."""
    backend = sys.modules.get("stock_watch_list_back_end")
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch.object(backend, 'set_request_cache_db', return_value="token"))
    stack.enter_context(patch.object(backend, 'normalize_groups_for_yfinance', side_effect=lambda g: g))
    stack.enter_context(patch.object(backend, 'get_cached_ticker_names', return_value={}))
    stack.enter_context(patch.object(backend, 'get_cached_stock_analysis', return_value={}))
    stack.enter_context(patch.object(backend, 'get_prices_with_cache', return_value=_mock_prices()))
    stack.enter_context(patch.object(backend, 'get_cached_betas', return_value={}))
    stack.enter_context(patch.object(backend, 'save_betas'))
    stack.enter_context(patch.object(backend, 'update_extended_hours_price_cache', return_value={}))
    stack.enter_context(patch.object(backend, 'get_market_date', return_value="2025-07-11"))
    mock_ctx = MagicMock()
    mock_ctx.reset = MagicMock()
    stack.enter_context(patch.object(backend, 'CURRENT_DB_PATH', mock_ctx))
    return stack


# ──────────────────────────────────────────────────────────────
# 1. 合法 POST JSON
# ──────────────────────────────────────────────────────────────
def test_post_valid_json(client, valid_payload):
    """POST with valid JSON returns success."""
    with _common_mocks():
        resp = client.post('/api/stock_data', json=valid_payload)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert isinstance(data["data"], list)


# ──────────────────────────────────────────────────────────────
# 2. 空 JSON
# ──────────────────────────────────────────────────────────────
def test_post_empty_json(client):
    """POST with empty JSON object returns 400."""
    resp = client.post('/api/stock_data', json={})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False
    assert "groups" in data["error"]


# ──────────────────────────────────────────────────────────────
# 3. 非 JSON 请求
# ──────────────────────────────────────────────────────────────
def test_post_non_json(client):
    """POST with non-JSON body returns 400."""
    resp = client.post(
        '/api/stock_data',
        data="this is not json",
        content_type='text/plain'
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


# ──────────────────────────────────────────────────────────────
# 4. 缺失必要字段
# ──────────────────────────────────────────────────────────────
def test_post_missing_groups(client):
    """POST without groups field returns 400."""
    resp = client.post('/api/stock_data', json={"broad_market_tickers": ["^GSPC"]})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False
    assert "groups" in data["error"]


# ──────────────────────────────────────────────────────────────
# 5. 字段类型错误
# ──────────────────────────────────────────────────────────────
def test_post_wrong_type_groups(client):
    """POST with groups as string (not dict) returns 400."""
    resp = client.post('/api/stock_data', json={"groups": "AAPL"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


def test_post_wrong_type_broad_market(client):
    """POST with broad_market_tickers as string returns 400."""
    resp = client.post('/api/stock_data', json={
        "groups": {"Tech": ["AAPL"]},
        "broad_market_tickers": "not-a-list"
    })
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


# ──────────────────────────────────────────────────────────────
# 6. 超大请求
# ──────────────────────────────────────────────────────────────
def test_post_oversized_body(client):
    """POST with body exceeding MAX_STOCK_DATA_BODY_SIZE returns 413."""
    big_ticker_list = ["AAPL"] * 500000  # ~2.5 MB when JSON-serialized
    resp = client.post('/api/stock_data', json={
        "groups": {"Big": big_ticker_list}
    })
    assert resp.status_code == 413
    data = resp.get_json()
    assert data["success"] is False


# ──────────────────────────────────────────────────────────────
# 7. 正常响应结构与旧行为一致
# ──────────────────────────────────────────────────────────────
def test_post_response_structure(client, valid_payload):
    """POST response has the same structure as the old GET response."""
    with _common_mocks():
        resp = client.post('/api/stock_data', json=valid_payload)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert isinstance(data["data"], list)
    if data["data"]:
        row = data["data"][0]
        assert "Ticker" in row
        assert "Price" in row


# ──────────────────────────────────────────────────────────────
# 8. 内部客户端确实使用 POST
# ──────────────────────────────────────────────────────────────
def test_internal_clients_use_post():
    """Verify that client source code uses POST, not GET, for /api/stock_data."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    files_to_check = {
        "app_streamlit.py": "requests.post",
        "app_streamlit_multiuser.py": "requests.post",
        "app_tkinter.py": "requests.post",
    }

    for filename, expected_pattern in files_to_check.items():
        filepath = os.path.join(project_root, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        assert expected_pattern in content, \
            f"{filename} should use {expected_pattern} for /api/stock_data"

        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "/api/stock_data" in line and "requests.get" in line:
                pytest.fail(
                    f"{filename}:{i+1} still uses requests.get for /api/stock_data"
                )


# ──────────────────────────────────────────────────────────────
# 9. GET 兼容行为和弃用提示
# ──────────────────────────────────────────────────────────────
def test_get_deprecated_header(client, valid_payload):
    """GET /api/stock_data still works but returns X-Deprecated header."""
    params = {
        "groups": json.dumps(valid_payload["groups"]),
        "broad_market_tickers": json.dumps(valid_payload["broad_market_tickers"]),
    }

    with _common_mocks():
        resp = client.get('/api/stock_data', query_string=params)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True

    assert "X-Deprecated" in resp.headers
    assert "deprecated" in resp.headers["X-Deprecated"].lower()


def test_get_empty_groups_deprecated(client):
    """GET with empty groups returns error (backward compatible)."""
    resp = client.get('/api/stock_data', query_string={"groups": "{}"})
    assert resp.status_code == 200  # old behavior returns 200 with success=False
    data = resp.get_json()
    assert data["success"] is False
    assert "X-Deprecated" in resp.headers


# ──────────────────────────────────────────────────────────────
# 10. P0 和 P1-2 测试不回归 (smoke check)
# ──────────────────────────────────────────────────────────────
def test_post_cache_key_in_json_body(client):
    """POST with cache_key in JSON body works correctly (P1-2 compatibility)."""
    flask = sys.modules["flask"]
    payload = {
        "groups": {"Tech": ["AAPL"]},
        "broad_market_tickers": [],
        "cache_key": "test_user_123"
    }

    captured_key = []

    def fake_set_cache_db():
        json_body = flask.request.get_json(silent=True)
        if isinstance(json_body, dict):
            ck = json_body.get("cache_key", "")
            if isinstance(ck, str):
                captured_key.append(ck)
        return "token"

    backend = sys.modules["stock_watch_list_back_end"]
    with patch.object(backend, 'set_request_cache_db', side_effect=fake_set_cache_db), \
         patch.object(backend, 'normalize_groups_for_yfinance', side_effect=lambda g: g), \
         patch.object(backend, 'get_cached_ticker_names', return_value={}), \
         patch.object(backend, 'get_cached_stock_analysis', return_value={}), \
         patch.object(backend, 'get_prices_with_cache', return_value=_mock_prices()), \
         patch.object(backend, 'get_cached_betas', return_value={}), \
         patch.object(backend, 'save_betas'), \
         patch.object(backend, 'update_extended_hours_price_cache', return_value={}), \
         patch.object(backend, 'get_market_date', return_value="2025-07-11"), \
         patch.object(backend, 'CURRENT_DB_PATH') as mock_ctx:

        mock_ctx.reset = MagicMock()
        resp = client.post('/api/stock_data', json=payload)

    assert resp.status_code == 200
    assert len(captured_key) == 1
    assert captured_key[0] == "test_user_123"


def test_post_null_broad_market_tickers(client):
    """POST with broad_market_tickers=null is treated as empty list."""
    payload = {
        "groups": {"Tech": ["AAPL"]},
        "broad_market_tickers": None,
    }

    with _common_mocks():
        resp = client.post('/api/stock_data', json=payload)

    assert resp.status_code == 200


def test_post_json_array_not_object(client):
    """POST with JSON array (not object) returns 400."""
    resp = client.post('/api/stock_data', json=["AAPL", "MSFT"])
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
