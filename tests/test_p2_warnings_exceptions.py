"""P2-12: Warnings and exception handling tests.

Verifies:
1. No global ``warnings.filterwarnings('ignore')`` in production code.
2. No bare ``except Exception: pass`` in production code.
3. ``except Exception:`` is not used as a catch-all for silently swallowed errors
   (``except Exception as e:`` that logs/re-raises is acceptable).
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

# ── Paths ──────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Production source files that should not have global warning suppression
# or bare ``except Exception: pass``.
_PROD_FILES = [
    "stock_watch_list_back_end.py",
    "app_streamlit.py",
    "app_streamlit_multiuser.py",
    "app_tkinter.py",
    "market_data_service.py",
    "multiuser_store.py",
    "daily_report/scripts/fetch_and_calc.py",
    "daily_report/scripts/gen_chart.py",
    "daily_report/mailer.py",
    "daily_report/service.py",
    "daily_report/worker.py",
    "daily_report/jobs.py",
    "daily_report/src/stock_daily_agent/tools.py",
    "daily_report/src/stock_daily_agent/agent_runner.py",
    "daily_report/src/stock_daily_agent/cli.py",
    "daily_report/src/stock_daily_agent/config.py",
]

# Files exempt from the ``except Exception:`` ban — top-level boundary handlers
# where a catch-all is the correct pattern (process must not crash).
_EXEMPT_EXCEPT_FILES = {
    "daily_report/service.py",   # generate_report top-level
    "daily_report/worker.py",    # worker top-level
    "daily_report/jobs.py",      # DB rollback+raise
}


def _read_file(rel_path: str) -> str:
    return (_PROJECT_ROOT / rel_path).read_text(encoding="utf-8")


def _existing_prod_files() -> list[str]:
    """Return rel_paths for all existing production files."""
    result = []
    for rel in _PROD_FILES:
        path = _PROJECT_ROOT / rel
        if path.exists():
            result.append(rel)
    return result


# ════════════════════════════════════════════════════════════════════
# Part 1: No global warnings.filterwarnings('ignore')
# ════════════════════════════════════════════════════════════════════

class TestNoGlobalWarningSuppression:
    """Production code must not globally suppress all warnings."""

    @pytest.mark.parametrize("rel_path", _existing_prod_files())
    def test_no_global_filterwarnings_ignore(self, rel_path):
        """No ``warnings.filterwarnings('ignore')`` in production code."""
        source = _read_file(rel_path)
        assert "filterwarnings('ignore')" not in source, (
            f"{rel_path}: global warnings.filterwarnings('ignore') found — "
            "P2-12 requires removing blanket warning suppression from production code"
        )
        assert 'filterwarnings("ignore")' not in source, (
            f"{rel_path}: global warnings.filterwarnings('ignore') found — "
            "P2-12 requires removing blanket warning suppression from production code"
        )

    def test_stock_watch_list_back_end_no_warnings_import_only(self):
        """If warnings is imported, it must not be used for blanket suppression."""
        source = _read_file("stock_watch_list_back_end.py")
        if "import warnings" in source:
            # If warnings is imported, filterwarnings must not be called with 'ignore'
            lines = source.splitlines()
            for i, line in enumerate(lines):
                stripped = line.strip()
                if "filterwarnings" in stripped and "ignore" in stripped:
                    pytest.fail(
                        f"Line {i+1}: blanket filterwarnings('ignore') found"
                    )

    def test_fetch_and_calc_no_warnings_suppression(self):
        """fetch_and_calc.py must not suppress warnings globally."""
        source = _read_file("daily_report/scripts/fetch_and_calc.py")
        assert "filterwarnings" not in source, (
            "fetch_and_calc.py still has filterwarnings"
        )

    def test_gen_chart_no_warnings_suppression(self):
        """gen_chart.py must not suppress warnings globally."""
        source = _read_file("daily_report/scripts/gen_chart.py")
        assert "filterwarnings" not in source, (
            "gen_chart.py still has filterwarnings"
        )


# ════════════════════════════════════════════════════════════════════
# Part 2: No bare ``except Exception: pass``
# ════════════════════════════════════════════════════════════════════

class TestNoBareExceptExceptionPass:
    """No ``except Exception: pass`` that silently swallows errors."""

    @pytest.mark.parametrize("rel_path", _existing_prod_files())
    def test_no_except_exception_pass(self, rel_path):
        """No ``except Exception:\\n    pass`` in production code."""
        source = _read_file(rel_path)
        # Match "except Exception:" followed by only whitespace/newline then "pass"
        pattern = r"except\s+Exception\s*:\s*\n\s*pass"
        matches = re.findall(pattern, source)
        assert len(matches) == 0, (
            f"{rel_path}: found {len(matches)} 'except Exception: pass' — "
            "P2-12 requires specific exception types instead of bare Exception"
        )

    @pytest.mark.parametrize("rel_path", _existing_prod_files())
    def test_no_except_exception_return(self, rel_path):
        """No ``except Exception:\\n    return ...`` in production code (except exempt files)."""
        if rel_path in _EXEMPT_EXCEPT_FILES:
            pytest.skip(f"{rel_path} is exempt (top-level boundary handler)")
        source = _read_file(rel_path)
        # Match "except Exception:" followed by whitespace/newline then "return"
        pattern = r"except\s+Exception\s*:\s*\n\s*return\b"
        matches = re.findall(pattern, source)
        assert len(matches) == 0, (
            f"{rel_path}: found {len(matches)} 'except Exception: return' — "
            "P2-12 requires specific exception types instead of bare Exception"
        )


# ════════════════════════════════════════════════════════════════════
# Part 3: Specific exception types are used
# ════════════════════════════════════════════════════════════════════

class TestSpecificExceptionTypes:
    """Verify that key functions use specific exception types."""

    def test_fetch_yfinance_ticker_name_uses_specific(self):
        """_fetch_yfinance_ticker_name catches specific exceptions, not bare Exception."""
        source = _read_file("stock_watch_list_back_end.py")
        func_match = re.search(
            r"def _fetch_yfinance_ticker_name\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find _fetch_yfinance_ticker_name function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "_fetch_yfinance_ticker_name still uses bare except Exception"
        )
        assert "KeyError" in func_body or "ValueError" in func_body, (
            "_fetch_yfinance_ticker_name should catch specific exceptions like KeyError/ValueError"
        )

    def test_fetch_yfinance_market_cap_uses_specific(self):
        """_fetch_yfinance_market_cap catches specific exceptions."""
        source = _read_file("stock_watch_list_back_end.py")
        func_match = re.search(
            r"def _fetch_yfinance_market_cap\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find _fetch_yfinance_market_cap function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "_fetch_yfinance_market_cap still uses bare except Exception"
        )

    def test_get_ticker_currency_uses_specific(self):
        """get_ticker_currency in multiuser app catches specific exceptions."""
        source = _read_file("app_streamlit_multiuser.py")
        func_match = re.search(
            r"def get_ticker_currency\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find get_ticker_currency function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "get_ticker_currency still uses bare except Exception"
        )

    def test_fetch_fear_greed_uses_specific(self):
        """fetch_fear_greed catches RequestException, not bare Exception."""
        source = _read_file("app_streamlit_multiuser.py")
        func_match = re.search(
            r"def fetch_fear_greed\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find fetch_fear_greed function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "fetch_fear_greed still uses bare except Exception"
        )
        assert "RequestException" in func_body, (
            "fetch_fear_greed should catch requests.RequestException"
        )

    def test_fetch_kline_data_uses_specific(self):
        """fetch_kline_data catches RequestException, not bare Exception."""
        source = _read_file("app_streamlit_multiuser.py")
        func_match = re.search(
            r"def fetch_kline_data\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find fetch_kline_data function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "fetch_kline_data still uses bare except Exception"
        )
        assert "RequestException" in func_body, (
            "fetch_kline_data should catch requests.RequestException"
        )

    def test_fetch_stock_data_uses_specific(self):
        """fetch_stock_data catches RequestException, not bare Exception."""
        source = _read_file("app_streamlit_multiuser.py")
        func_match = re.search(
            r"def fetch_stock_data\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find fetch_stock_data function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "fetch_stock_data still uses bare except Exception"
        )

    def test_fetch_breadth_data_uses_specific(self):
        """fetch_breadth_data catches RequestException, not bare Exception."""
        source = _read_file("app_streamlit_multiuser.py")
        func_match = re.search(
            r"def fetch_breadth_data\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find fetch_breadth_data function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "fetch_breadth_data still uses bare except Exception"
        )

    def test_pickle_load_uses_specific(self):
        """pickle.load catches UnpicklingError, not bare Exception."""
        source = _read_file("market_data_service.py")
        assert "except Exception:" not in source or \
               "except (pickle.UnpicklingError, EOFError, OSError, ValueError)" in source, (
            "market_data_service.py should catch specific pickle exceptions"
        )

    def test_extract_json_payload_uses_specific(self):
        """_extract_json_payload catches JSONDecodeError, not bare Exception."""
        source = _read_file("daily_report/src/stock_daily_agent/tools.py")
        func_match = re.search(
            r"def _extract_json_payload\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find _extract_json_payload function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "_extract_json_payload still uses bare except Exception"
        )
        assert "JSONDecodeError" in func_body, (
            "_extract_json_payload should catch json.JSONDecodeError"
        )

    def test_safe_float_value_uses_specific(self):
        """_safe_float_value catches ValueError/TypeError, not bare Exception."""
        source = _read_file("daily_report/src/stock_daily_agent/tools.py")
        func_match = re.search(
            r"def _safe_float_value\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find _safe_float_value function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "_safe_float_value still uses bare except Exception"
        )
        assert "ValueError" in func_body, (
            "_safe_float_value should catch ValueError/TypeError"
        )

    def test_import_fallback_uses_import_error(self):
        """Import fallbacks use ImportError, not bare Exception."""
        # fetch_and_calc.py
        source = _read_file("daily_report/scripts/fetch_and_calc.py")
        assert "except ImportError:" in source, (
            "fetch_and_calc.py import fallback should use ImportError"
        )

    def test_qwen_agent_import_uses_import_error(self):
        """qwen_agent import fallback uses ImportError, not bare Exception."""
        source = _read_file("daily_report/src/stock_daily_agent/tools.py")
        assert "except ImportError:" in source, (
            "tools.py qwen_agent import fallback should use ImportError"
        )

    def test_smtp_configured_uses_specific(self):
        """smtp_configured catches RuntimeError/ValueError, not bare Exception."""
        source = _read_file("daily_report/mailer.py")
        func_match = re.search(
            r"def smtp_configured\(.*?\n((?:.*\n)*?)(?=\ndef |\Z)",
            source,
        )
        assert func_match, "Could not find smtp_configured function"
        func_body = func_match.group(1)
        assert "except Exception:" not in func_body, (
            "smtp_configured still uses bare except Exception"
        )
        assert "RuntimeError" in func_body, (
            "smtp_configured should catch RuntimeError/ValueError"
        )


# ════════════════════════════════════════════════════════════════════
# Part 4: Exception handlers in DB operations are appropriate
# ════════════════════════════════════════════════════════════════════

class TestDatabaseExceptionHandlers:
    """DB-related exception handlers should be appropriate."""

    def test_multiuser_store_rollback_uses_sqlite_error(self):
        """multiuser_store.py ROLLBACK fallback catches sqlite3.Error."""
        source = _read_file("multiuser_store.py")
        # The ROLLBACK except should catch sqlite3.Error, not bare Exception
        assert "except sqlite3.Error:" in source, (
            "multiuser_store.py should catch sqlite3.Error for ROLLBACK fallback"
        )


# ════════════════════════════════════════════════════════════════════
# Part 5: Behavioral tests — specific exceptions still work
# ════════════════════════════════════════════════════════════════════

class TestExceptionHandlerBehavior:
    """Verify that specific exception handlers still catch the expected errors."""

    def test_safe_float_value_catches_value_error(self):
        """_safe_float_value returns default on ValueError."""
        source = _read_file("daily_report/src/stock_daily_agent/tools.py")
        func_match = re.search(
            r"(def _safe_float_value\(.*?\n((?:.*\n)*?))(?=\ndef |\Z)",
            source,
        )
        assert func_match
        ns: dict = {}
        exec(textwrap.dedent(func_match.group(1)), ns)
        func = ns["_safe_float_value"]

        assert func("abc") == 0.0
        assert func(None) == 0.0
        assert func("") == 0.0
        assert func("3.14") == 3.14
        assert func(42) == 42.0

    def test_clamp_score_catches_value_error(self):
        """_clamp_score returns 50.0 on ValueError."""
        source = _read_file("daily_report/src/stock_daily_agent/tools.py")
        func_match = re.search(
            r"(def _clamp_score\(.*?\n((?:.*\n)*?))(?=\ndef |\Z)",
            source,
        )
        assert func_match
        ns: dict = {}
        exec(textwrap.dedent(func_match.group(1)), ns)
        func = ns["_clamp_score"]

        assert func("abc") == 50.0
        assert func(None) == 50.0
        assert func(75.0) == 75.0
        assert func(150.0) == 100.0
        assert func(-10.0) == 0.0

    def test_extract_json_payload_handles_invalid_json(self):
        """_extract_json_payload returns None for invalid JSON."""
        source = _read_file("daily_report/src/stock_daily_agent/tools.py")
        func_match = re.search(
            r"(def _extract_json_payload\(.*?\n((?:.*\n)*?))(?=\ndef |\Z)",
            source,
        )
        assert func_match
        # strip_markdown_code_fence is imported from utils — provide it inline
        def strip_markdown_code_fence(text: str) -> str:
            import re as _re
            text = text.strip()
            if text.startswith("```"):
                text = _re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
                text = _re.sub(r"\s*```$", "", text)
            return text.strip()

        ns: dict = {"json": __import__("json"), "strip_markdown_code_fence": strip_markdown_code_fence}
        exec(textwrap.dedent(func_match.group(1)), ns)
        func = ns["_extract_json_payload"]

        assert func("not json at all") is None
        assert func('{"key": "value"}') == {"key": "value"}
        assert func('```json\n{"key": "value"}\n```') == {"key": "value"}

    def test_source_domain_handles_invalid_url(self):
        """_source_domain returns empty string for invalid input."""
        source = _read_file("daily_report/src/stock_daily_agent/tools.py")
        func_match = re.search(
            r"(def _source_domain\(.*?\n((?:.*\n)*?))(?=\ndef |\Z)",
            source,
        )
        assert func_match
        ns: dict = {"urlparse": __import__("urllib.parse", fromlist=["urlparse"]).urlparse}
        exec(textwrap.dedent(func_match.group(1)), ns)
        func = ns["_source_domain"]

        assert func("") == ""
        assert func("https://www.example.com/path") == "example.com"
        assert func("not a url") == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
