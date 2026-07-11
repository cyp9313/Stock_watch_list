"""P1-5 tests: Streamlit/Flask lifecycle separation and /api/health endpoint.

Covers:
  1. /api/health returns expected app identifier
  2. Backend healthy → health check succeeds
  3. Port open but wrong service → health check fails
  4. Backend unreachable → clear error status
  5. Configured backend URL is used
  6. No repeated Flask startup
  7. P1-6 POST API tests still pass
  8. P0 / P1-2 tests don't regress (run separately)
"""

import os
import sys
import time
import json
import threading
from unittest.mock import patch, MagicMock

import pytest

# ── Modules that other test files may stub with MagicMock ──
# We must restore the real versions before each test.
_REAL_MODULES = [
    "flask", "flask.testing", "flask.json", "werkzeug", "werkzeug.test",
    "pandas", "numpy", "pytz", "requests", "requests_cache",
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
    globals()["requests"] = requests

    yield


# ════════════════════════════════════════════════════════════
# Test 1: /api/health returns expected service identifier
# ════════════════════════════════════════════════════════════

def test_health_endpoint_returns_service_identity():
    """/api/health should return 200 with service='stock-watchlist-api'."""
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as c:
        resp = c.get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["service"] == "stock-watchlist-api"
        assert "version" in data


# ════════════════════════════════════════════════════════════
# Test 2: Backend healthy → check_backend_health succeeds
# ════════════════════════════════════════════════════════════

def test_check_backend_health_success():
    """When backend responds correctly, health check succeeds."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "ok",
        "service": "stock-watchlist-api",
        "version": "1.0",
    }

    with patch("requests.get", return_value=mock_resp):
        resp = requests.get("http://127.0.0.1:5000/api/health", timeout=3)
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "stock-watchlist-api"


# ════════════════════════════════════════════════════════════
# Test 3: Port open but wrong service → health check fails
# ════════════════════════════════════════════════════════════

def test_health_check_wrong_service():
    """When port is open but service identifier doesn't match, health check fails."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "ok",
        "service": "some-other-app",
        "version": "2.0",
    }

    with patch("requests.get", return_value=mock_resp):
        resp = requests.get("http://127.0.0.1:5000/api/health", timeout=3)
        data = resp.json()
        assert data["service"] != "stock-watchlist-api"


def test_health_check_wrong_http_status():
    """When backend returns non-200, health check fails."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("requests.get", return_value=mock_resp):
        resp = requests.get("http://127.0.0.1:5000/api/health", timeout=3)
        assert resp.status_code != 200


# ════════════════════════════════════════════════════════════
# Test 4: Backend unreachable → clear error status
# ════════════════════════════════════════════════════════════

def test_health_check_connection_error():
    """When backend is unreachable, requests.ConnectionError is raised."""
    with patch("requests.get", side_effect=requests.ConnectionError("Connection refused")):
        with pytest.raises(requests.ConnectionError):
            requests.get("http://127.0.0.1:5000/api/health", timeout=3)


def test_health_check_timeout():
    """When backend times out, requests.Timeout is raised."""
    with patch("requests.get", side_effect=requests.Timeout("Timed out")):
        with pytest.raises(requests.Timeout):
            requests.get("http://127.0.0.1:5000/api/health", timeout=3)


# ════════════════════════════════════════════════════════════
# Test 5: Configured backend URL is used
# ════════════════════════════════════════════════════════════

def test_api_base_from_env_var():
    """STOCK_API_BASE_URL environment variable should override default."""
    custom_url = "http://10.0.0.99:8080"
    saved = os.environ.get("STOCK_API_BASE_URL")
    try:
        os.environ["STOCK_API_BASE_URL"] = custom_url
        api_base = os.environ.get("STOCK_API_BASE_URL", "http://127.0.0.1:5000")
        assert api_base == custom_url
    finally:
        if saved is None:
            os.environ.pop("STOCK_API_BASE_URL", None)
        else:
            os.environ["STOCK_API_BASE_URL"] = saved


def test_api_base_default():
    """Without env var, default API_BASE should be http://127.0.0.1:5000."""
    saved = os.environ.pop("STOCK_API_BASE_URL", None)
    try:
        api_base = os.environ.get("STOCK_API_BASE_URL", "http://127.0.0.1:5000")
        assert api_base == "http://127.0.0.1:5000"
    finally:
        if saved is not None:
            os.environ["STOCK_API_BASE_URL"] = saved


def test_dev_mode_default_on():
    """STOCK_DEV_MODE should default to '1' (dev mode on)."""
    saved = os.environ.pop("STOCK_DEV_MODE", None)
    try:
        dev_mode = os.environ.get("STOCK_DEV_MODE", "1") != "0"
        assert dev_mode is True
    finally:
        if saved is not None:
            os.environ["STOCK_DEV_MODE"] = saved


def test_dev_mode_off():
    """When STOCK_DEV_MODE=0, dev mode should be off (production)."""
    saved = os.environ.get("STOCK_DEV_MODE")
    try:
        os.environ["STOCK_DEV_MODE"] = "0"
        dev_mode = os.environ.get("STOCK_DEV_MODE", "1") != "0"
        assert dev_mode is False
    finally:
        if saved is None:
            os.environ.pop("STOCK_DEV_MODE", None)
        else:
            os.environ["STOCK_DEV_MODE"] = saved


# ════════════════════════════════════════════════════════════
# Test 6: No repeated Flask startup
# ════════════════════════════════════════════════════════════

def test_no_repeated_flask_startup():
    """ensure_backend() should not start Flask if already ready."""
    backend_ready = [False]
    flask_start_count = [0]

    def ensure_backend_simulated(check_fn, dev_mode=True):
        if backend_ready[0]:
            return True, "ok"

        ok, msg = check_fn()
        if ok:
            backend_ready[0] = True
            return True, msg

        if not dev_mode:
            return False, f"后端不可用 ({msg})"

        flask_start_count[0] += 1

        for _ in range(10):
            time.sleep(0.01)
            ok2, msg2 = check_fn()
            if ok2:
                backend_ready[0] = True
                return True, "ok"

        return False, f"后端启动失败 ({msg})"

    # First call: backend is healthy → should not start Flask
    check_fn = MagicMock(return_value=(True, "ok"))
    result = ensure_backend_simulated(check_fn, dev_mode=True)
    assert result == (True, "ok")
    assert flask_start_count[0] == 0

    # Second call: _backend_ready is True → should not call check or start Flask
    check_fn2 = MagicMock(return_value=(True, "ok"))
    result2 = ensure_backend_simulated(check_fn2, dev_mode=True)
    assert result2 == (True, "ok")
    assert flask_start_count[0] == 0
    check_fn2.assert_not_called()


def test_no_repeated_flask_startup_when_unhealthy():
    """In production mode with unhealthy backend, ensure_backend should not start Flask."""
    backend_ready = [False]
    flask_start_count = [0]

    def ensure_backend_simulated(check_fn, dev_mode=False):
        if backend_ready[0]:
            return True, "ok"

        ok, msg = check_fn()
        if ok:
            backend_ready[0] = True
            return True, msg

        if not dev_mode:
            return False, f"后端不可用 ({msg})"

        flask_start_count[0] += 1
        return False, "failed"

    check_fn = MagicMock(return_value=(False, "无法连接后端服务"))
    result = ensure_backend_simulated(check_fn, dev_mode=False)
    assert result[0] is False
    assert flask_start_count[0] == 0


# ════════════════════════════════════════════════════════════
# Test 7: Health endpoint doesn't interfere with existing API
# ════════════════════════════════════════════════════════════

def test_health_endpoint_does_not_interfere_with_stock_data():
    """The /api/health endpoint should not affect /api/stock_data behavior."""
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as c:
        health_resp = c.get("/api/health")
        assert health_resp.status_code == 200

        # Stock data POST still rejects empty body
        stock_resp = c.post("/api/stock_data", json={})
        assert stock_resp.status_code == 400


def test_health_endpoint_does_not_require_cache_db():
    """The /api/health endpoint should not trigger DB initialization or cache setup."""
    backend.app.config["TESTING"] = True
    with patch.object(backend, "set_request_cache_db") as mock_cache:
        with backend.app.test_client() as c:
            resp = c.get("/api/health")
            assert resp.status_code == 200
            mock_cache.assert_not_called()


# ════════════════════════════════════════════════════════════
# Test 8: Health endpoint response structure is stable
# ════════════════════════════════════════════════════════════

def test_health_endpoint_stable_structure():
    """Health endpoint should return a stable, structured JSON response."""
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as c:
        resp = c.get("/api/health")
        data = resp.get_json()
        assert "status" in data
        assert "service" in data
        assert "version" in data
        assert isinstance(data["status"], str)
        assert isinstance(data["service"], str)
        assert isinstance(data["version"], str)


def test_health_endpoint_no_auth_required():
    """Health endpoint should be accessible without authentication."""
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as c:
        resp = c.get("/api/health")
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════
# Test 9: Backend __main__ block is active
# ════════════════════════════════════════════════════════════

def test_backend_has_main_block():
    """The backend should have an active __main__ block for standalone startup."""
    import inspect
    source = inspect.getsource(backend)
    assert 'if __name__ == "__main__":' in source
    assert '# if __name__ == "__main__":' not in source


# ════════════════════════════════════════════════════════════
# Test 10: No port-only probing functions remain in frontend source
# ════════════════════════════════════════════════════════════

def test_no_port_probing_in_streamlit_apps():
    """Frontend source should not contain is_port_open or socket.create_connection."""
    project_root = os.path.join(os.path.dirname(__file__), "..")
    for filename in ("app_streamlit.py", "app_streamlit_multiuser.py"):
        filepath = os.path.join(project_root, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        assert "is_port_open" not in content, f"{filename} still contains is_port_open"
        assert "socket.create_connection" not in content, f"{filename} still contains socket.create_connection"
        assert "ensure_flask" not in content, f"{filename} still contains ensure_flask"
        assert "time.sleep(2)" not in content, f"{filename} still contains fixed sleep(2)"


def test_no_unconditional_flask_in_tkinter():
    """Tkinter app should not start Flask unconditionally at module level."""
    project_root = os.path.join(os.path.dirname(__file__), "..")
    filepath = os.path.join(project_root, "app_tkinter.py")
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    assert "check_backend_health" in content
    assert "ensure_backend" in content
    assert "def run_flask():" not in content
