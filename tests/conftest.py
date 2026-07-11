"""Shared pytest configuration and fixtures.

This file is automatically loaded by pytest before any test module.
It provides:
  * Project root in sys.path (so test files don't need to repeat it)
  * Module cleanup fixture (restores real modules after stub tests)
  * Common temp-database fixture
  * Marker-based test categorization helpers
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Project root in sys.path ─────────────────────────────────────────
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── Modules that other test files may stub with MagicMock ────────────
# These need to be restored before tests that require real implementations.
_STUBBABLE_MODULES = [
    "flask", "flask.testing", "flask.json", "werkzeug", "werkzeug.test",
    "pandas", "numpy", "pytz", "requests", "requests.sessions",
    "requests.adapters", "requests_cache",
    "fear_and_greed", "dotenv", "concurrent", "concurrent.futures",
    "stockanalysis_scraper", "ticker_mapping", "stock_watch_list_back_end",
    "config_loader",
]


@pytest.fixture(autouse=True)
def _restore_real_modules():
    """Restore real modules before each test.

    Some test files (e.g. test_db_init_separation.py) stub flask/pandas/etc.
    as MagicMock in sys.modules. This fixture undoes that so subsequent tests
    get real implementations.

    This is autouse=True so it runs before every test automatically.
    """
    # Pop modules that are MagicMock stubs
    for _m in _STUBBABLE_MODULES:
        mod = sys.modules.get(_m)
        if mod is None:
            continue
        if _m == "stock_watch_list_back_end" or isinstance(mod, MagicMock):
            sys.modules.pop(_m, None)

    # Pop submodules of requests/requests_cache so reimport re-binds
    # package-level attributes.
    for _prefix in ("requests.", "requests_cache."):
        for _k in list(sys.modules):
            if _k.startswith(_prefix):
                sys.modules.pop(_k, None)

    yield

    # No teardown needed — the next test's setup will clean again


# ── Temporary database fixture ───────────────────────────────────────

@pytest.fixture
def temp_db_path(tmp_path) -> str:
    """Provide a temporary SQLite database path.

    Sets REPORT_JOB_DB to a temp file and cleans up after.
    """
    db_file = str(tmp_path / "test_jobs.db")
    old_val = os.environ.get("REPORT_JOB_DB")
    os.environ["REPORT_JOB_DB"] = db_file
    try:
        yield db_file
    finally:
        if old_val is not None:
            os.environ["REPORT_JOB_DB"] = old_val
        else:
            os.environ.pop("REPORT_JOB_DB", None)


# ── Isolated environment fixture ─────────────────────────────────────

@pytest.fixture
def clean_env(monkeypatch):
    """Provide a clean environment for config testing.

    Removes all REPORT_*, STOCK_*, LOGIN_* vars to test default behavior.
    """
    for key in list(os.environ.keys()):
        if key.startswith(("REPORT_", "STOCK_", "LOGIN_", "SEARXNG_", "SERPER_",
                           "ARTICLE_", "EVIDENCE_", "DASHSCOPE_", "DEEPSEEK_",
                           "LLM_", "QWEN_", "OPENAI_", "SEARCH_",
                           "ENABLE_", "ALLOW_", "STOCKANALYSIS_")):
            monkeypatch.delenv(key, raising=False)
    yield


# ── No-network marker ────────────────────────────────────────────────

def pytest_collection_modifyitems(config, items):
    """Automatically mark tests that don't need network access as 'unit'."""
    for item in items:
        # If no explicit marker, try to categorize
        if not any(item.iter_markers()):
            # Source-based tests (reading .py files and checking patterns)
            # are unit tests
            if hasattr(item, "function"):
                func_name = item.function.__name__
                if any(keyword in func_name for keyword in
                       ("source", "structure", "exists", "no_", "has_", "uses_")):
                    item.add_marker(pytest.mark.unit)
