"""P2-5 tests: Watchlist failure isolation -- page decoupling.

Verifies that:
1. st.stop() is completely removed from app_streamlit_multiuser.py
2. fetch_stock_data handles connection errors gracefully (returns dict, not crash)
3. fetch_kline_data handles connection errors gracefully (returns None, not crash)
4. Backend failure shows warning banner, not error + st.stop()
5. Stock data failure still creates all 4 tabs (including AI Agent Reports)
6. AI Agent Reports tab content does not depend on stock data
7. Backend failure does not block AI Agent Reports tab
8. fetch_stock_data still returns data on success (no regression)
9. fetch_kline_data still returns data on success (no regression)
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = REPO_ROOT / "app_streamlit_multiuser.py"
_SOURCE = APP_FILE.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


# ---------------------------------------------------------------------------
# Helper: extract a function from source as executable code
# ---------------------------------------------------------------------------

def _extract_func_source(func_name: str) -> str:
    """Return the source code of a top-level function (including decorators)."""
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.unparse(node)
    raise ValueError(f"Function {func_name} not found in {APP_FILE}")


def _make_ns(**extra) -> dict:
    """Create a namespace for executing extracted functions."""
    import requests as _real_requests
    st_mock = MagicMock()
    st_mock.cache_data = lambda *a, **kw: lambda f: f
    st_mock.cache_resource = lambda *a, **kw: lambda f: f
    ns: dict = {
        "st": st_mock,
        "requests": _real_requests,
        "json": json,
        "API_BASE": "http://127.0.0.1:5000",
    }
    ns.update(extra)
    return ns


# ---------------------------------------------------------------------------
# Structural tests: verify code patterns in the source file
# ---------------------------------------------------------------------------

class TestNoStStop:
    """st.stop() must be completely removed from the multi-user app."""

    def test_no_st_stop_in_multiuser_app(self):
        """No st.stop() call anywhere in app_streamlit_multiuser.py."""
        assert "st.stop()" not in _SOURCE, (
            "st.stop() found in app_streamlit_multiuser.py -- "
            "P2-5 requires all st.stop() calls to be removed for page decoupling"
        )

    def test_no_st_stop_in_main_flow_section(self):
        """Specifically check the main app flow section (after line 2300)."""
        lines = _SOURCE.splitlines()
        for i, line in enumerate(lines, 1):
            if i >= 2300 and "st.stop()" in line:
                pytest.fail(f"st.stop() found at line {i}: {line.strip()}")


class TestFetchStockDataHasTryExcept:
    """fetch_stock_data must have try/except to handle backend failures."""

    def test_function_has_try_except(self):
        source = _extract_func_source("fetch_stock_data")
        assert "try:" in source, "fetch_stock_data must contain a try block"
        assert "except" in source, "fetch_stock_data must contain an except block"

    def test_returns_dict_on_error(self):
        """The except block must return a dict with success=False."""
        source = _extract_func_source("fetch_stock_data")
        assert '"success": False' in source or "'success': False" in source, (
            "fetch_stock_data except block must return {'success': False, ...}"
        )


class TestFetchKlineDataHasTryExcept:
    """fetch_kline_data must have try/except to handle backend failures."""

    def test_function_has_try_except(self):
        source = _extract_func_source("fetch_kline_data")
        assert "try:" in source, "fetch_kline_data must contain a try block"
        assert "except" in source, "fetch_kline_data must contain an except block"

    def test_returns_none_on_error(self):
        """The except block must return None."""
        source = _extract_func_source("fetch_kline_data")
        assert "return None" in source, (
            "fetch_kline_data except block must return None"
        )


class TestBackendFailureHandling:
    """Backend failure must show warning, not error + st.stop()."""

    def test_uses_warning_not_error_for_backend(self):
        """Backend failure should use st.warning (advisory), not st.error (blocking)."""
        # Find the backend check section
        assert "ensure_backend()" in _SOURCE
        # After our fix, the code should use st.warning for backend failure
        assert "st.warning" in _SOURCE, (
            "st.warning should be used for backend failure (advisory, non-blocking)"
        )

    def test_backend_failure_message_mentions_daily_report(self):
        """The warning message should inform users that AI daily report still works."""
        # Find the warning message near ensure_backend
        lines = _SOURCE.splitlines()
        for i, line in enumerate(lines):
            if "ensure_backend()" in line and "if not" in lines[i + 1] if i + 1 < len(lines) else False:
                # Check the next few lines for the warning
                section = "\n".join(lines[i:i + 6])
                if "st.warning" in section:
                    assert "日报" in section or "daily report" in section.lower() or "AI" in section, (
                        "Backend warning should mention that AI daily report is unaffected"
                    )
                    return
        # Also check a broader window
        for i, line in enumerate(lines):
            if "ensure_backend()" in line:
                section = "\n".join(lines[i:i + 8])
                if "st.warning" in section:
                    assert "日报" in section or "AI" in section, (
                        "Backend warning should mention AI daily report still works"
                    )
                    return
        pytest.fail("Could not find backend failure warning section")


class TestStockDataFailureCreatesAllTabs:
    """Stock data failure must not prevent any tab from being created."""

    def test_tabs_created_regardless_of_stock_data(self):
        """st.tabs() call must come after the stock data check, not inside an
        st.stop() branch."""
        lines = _SOURCE.splitlines()
        tabs_line_idx = None
        stock_stop_idx = None
        for i, line in enumerate(lines):
            if "st.tabs(" in line and "AI Agent Reports" in (lines[i] if i < len(lines) else ""):
                tabs_line_idx = i
            # Look for the old pattern: st.error + st.stop after stock data check
            if "stock_payload.get" in line:
                for j in range(i, min(i + 5, len(lines))):
                    if "st.stop()" in lines[j]:
                        stock_stop_idx = j
        assert tabs_line_idx is not None, "Could not find st.tabs() call with AI Agent Reports"
        assert stock_stop_idx is None, (
            f"st.stop() found near stock_payload check at line {stock_stop_idx + 1} -- "
            "stock data failure must not call st.stop()"
        )

    def test_stock_data_uses_graceful_degradation(self):
        """The code should use _stock_data_ok flag pattern, not st.stop()."""
        assert "_stock_data_ok" in _SOURCE, (
            "Stock data failure should use _stock_data_ok flag for graceful degradation"
        )
        assert "if not _stock_data_ok:" in _SOURCE or "if not _stock_data_ok :" in _SOURCE, (
            "Stock-dependent tabs should check _stock_data_ok before rendering"
        )

    def test_ai_agent_reports_tab_not_gated_by_stock_data(self):
        """The AI Agent Reports tab (main_tabs[3]) should not check _stock_data_ok."""
        lines = _SOURCE.splitlines()
        # Find the main_tabs[3] section
        ai_tab_idx = None
        for i, line in enumerate(lines):
            if "main_tabs[3]" in line:
                ai_tab_idx = i
                break
        assert ai_tab_idx is not None, "Could not find main_tabs[3] (AI Agent Reports tab)"
        # Check the next few lines -- should NOT contain _stock_data_ok check
        section = "\n".join(lines[ai_tab_idx:ai_tab_idx + 5])
        assert "_stock_data_ok" not in section, (
            "AI Agent Reports tab must not be gated by _stock_data_ok -- "
            "it should render unconditionally"
        )
        assert "render_daily_report" in section, (
            "AI Agent Reports tab should call render_daily_report(user)"
        )

    def test_all_four_tabs_exist(self):
        """All 4 tabs must be created: Stocks, Broad Market, Market Breadth, AI Agent Reports."""
        assert "main_tabs[0]" in _SOURCE, "Tab 0 (Stocks) missing"
        assert "main_tabs[1]" in _SOURCE, "Tab 1 (Broad Market) missing"
        assert "main_tabs[2]" in _SOURCE, "Tab 2 (Market Breadth) missing"
        assert "main_tabs[3]" in _SOURCE, "Tab 3 (AI Agent Reports) missing"


# ---------------------------------------------------------------------------
# Behavior tests: verify fetch functions handle errors correctly
# ---------------------------------------------------------------------------

class TestFetchStockDataBehavior:
    """Test fetch_stock_data error handling with mocked requests."""

    def _make_func(self, mock_requests):
        """Create a testable fetch_stock_data with mocked dependencies."""
        import requests as real_requests
        # P2-12: code now catches requests.RequestException specifically,
        # so the mock must expose the real exception class.
        mock_requests.RequestException = real_requests.RequestException
        from multiuser_store import normalize_config, config_to_api_groups, broad_market_tickers
        func_source = _extract_func_source("fetch_stock_data")
        ns = _make_ns(
            requests=mock_requests,
            normalize_config=normalize_config,
            config_to_api_groups=config_to_api_groups,
            broad_market_tickers=broad_market_tickers,
        )
        exec(func_source, ns)
        return ns["fetch_stock_data"]

    def test_returns_error_on_connection_error(self):
        """fetch_stock_data returns {'success': False} on ConnectionError."""
        import requests as real_requests
        mock_requests = MagicMock()
        mock_requests.post.side_effect = real_requests.ConnectionError("Connection refused")
        func = self._make_func(mock_requests)

        config_json = json.dumps({"stocks_pages": [{"name": "Test", "groups": {}}], "broad_pages": []})
        result = func(config_json, "")

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result
        assert "Backend request failed" in result["error"]

    def test_returns_error_on_timeout(self):
        """fetch_stock_data returns {'success': False} on Timeout."""
        import requests as real_requests
        mock_requests = MagicMock()
        mock_requests.post.side_effect = real_requests.Timeout("Request timed out")
        func = self._make_func(mock_requests)

        config_json = json.dumps({"stocks_pages": [{"name": "Test", "groups": {}}], "broad_pages": []})
        result = func(config_json, "")

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result

    def test_returns_error_on_http_error(self):
        """fetch_stock_data returns {'success': False} on non-200 HTTP status."""
        mock_requests = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_requests.post.return_value = mock_resp
        func = self._make_func(mock_requests)

        config_json = json.dumps({"stocks_pages": [{"name": "Test", "groups": {}}], "broad_pages": []})
        result = func(config_json, "")

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "HTTP 500" in result["error"]

    def test_returns_data_on_success(self):
        """fetch_stock_data returns the JSON payload on success (no regression)."""
        mock_requests = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "data": [{"Ticker": "AAPL"}]}
        mock_requests.post.return_value = mock_resp
        func = self._make_func(mock_requests)

        config_json = json.dumps({"stocks_pages": [{"name": "Test", "groups": {}}], "broad_pages": []})
        result = func(config_json, "user123")

        assert result["success"] is True
        assert "data" in result
        assert result["data"][0]["Ticker"] == "AAPL"

    def test_generic_exception_caught(self):
        """fetch_stock_data catches ValueError (e.g. JSON decode errors), not just requests errors."""
        mock_requests = MagicMock()
        mock_requests.post.side_effect = ValueError("JSON decode error")
        func = self._make_func(mock_requests)

        config_json = json.dumps({"stocks_pages": [{"name": "Test", "groups": {}}], "broad_pages": []})
        result = func(config_json, "")

        assert isinstance(result, dict)
        assert result["success"] is False


class TestFetchKlineDataBehavior:
    """Test fetch_kline_data error handling with mocked requests."""

    def _make_func(self, mock_requests):
        """Create a testable fetch_kline_data with mocked dependencies."""
        import requests as real_requests
        # P2-12: code now catches requests.RequestException specifically,
        # so the mock must expose the real exception class.
        mock_requests.RequestException = real_requests.RequestException
        func_source = _extract_func_source("fetch_kline_data")
        ns = _make_ns(requests=mock_requests)
        exec(func_source, ns)
        return ns["fetch_kline_data"]

    def test_returns_none_on_connection_error(self):
        """fetch_kline_data returns None on ConnectionError."""
        import requests as real_requests
        mock_requests = MagicMock()
        mock_requests.get.side_effect = real_requests.ConnectionError("Connection refused")
        func = self._make_func(mock_requests)

        result = func("AAPL", 365, "1d", "user123")

        assert result is None

    def test_returns_none_on_timeout(self):
        """fetch_kline_data returns None on Timeout."""
        import requests as real_requests
        mock_requests = MagicMock()
        mock_requests.get.side_effect = real_requests.Timeout("Request timed out")
        func = self._make_func(mock_requests)

        result = func("AAPL", 365, "1d", "")

        assert result is None

    def test_returns_none_on_http_error(self):
        """fetch_kline_data returns None on non-200 HTTP status."""
        mock_requests = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_requests.get.return_value = mock_resp
        func = self._make_func(mock_requests)

        result = func("AAPL", 365, "1d", "")

        assert result is None

    def test_returns_data_on_success(self):
        """fetch_kline_data returns JSON data on success (no regression)."""
        mock_requests = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "ohlc": {"close": [100, 101]}}
        mock_requests.get.return_value = mock_resp
        func = self._make_func(mock_requests)

        result = func("AAPL", 365, "1d", "user123")

        assert result is not None
        assert result["success"] is True

    def test_generic_exception_caught(self):
        """fetch_kline_data catches any Exception, not just requests errors."""
        mock_requests = MagicMock()
        mock_requests.get.side_effect = ValueError("Unexpected error")
        func = self._make_func(mock_requests)

        result = func("AAPL", 365, "1d", "")

        assert result is None


# ---------------------------------------------------------------------------
# Integration: verify AI Agent Reports does not depend on backend or stock data
# ---------------------------------------------------------------------------

class TestAIReportIndependence:
    """Verify that render_daily_report does not call the Flask backend."""

    def test_render_daily_report_does_not_use_api_base(self):
        """render_daily_report should not make HTTP calls to API_BASE."""
        source = _extract_func_source("render_daily_report")
        assert "API_BASE" not in source, (
            "render_daily_report should not reference API_BASE -- "
            "AI daily report has its own independent pipeline"
        )
        assert "/api/" not in source, (
            "render_daily_report should not call Flask backend API endpoints"
        )

    def test_render_daily_report_uses_local_pipeline(self):
        """render_daily_report should use generate_report from daily_report.service."""
        source = _extract_func_source("render_daily_report")
        assert "generate_report" in source, (
            "render_daily_report should call generate_report (local pipeline)"
        )
        assert "runtime_available" in source, (
            "render_daily_report should call runtime_available (local check)"
        )

    def test_render_daily_report_does_not_fetch_stock_data(self):
        """render_daily_report should not call fetch_stock_data."""
        source = _extract_func_source("render_daily_report")
        assert "fetch_stock_data" not in source, (
            "render_daily_report must not depend on fetch_stock_data"
        )

    def test_render_daily_report_does_not_call_ensure_backend(self):
        """render_daily_report should not call ensure_backend."""
        source = _extract_func_source("render_daily_report")
        assert "ensure_backend" not in source, (
            "render_daily_report must not depend on ensure_backend"
        )
