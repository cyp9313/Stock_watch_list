"""Tests for P2-7: Unified .env and configuration loading.

Covers:
  * CWD-independent project root derivation
  * .env loading from explicit path
  * .env loading from project root
  * Process env priority (not overridden by .env)
  * Missing .env graceful handling
  * Required config validation
  * Typed config helpers (int, float, bool)
  * .env.example completeness
  * All entry points use unified loader
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config_loader import (
    PROJECT_ROOT,
    ConfigError,
    _parse_env_line,
    load_project_env,
    get_config,
    get_config_int,
    get_config_float,
    get_config_bool,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_env_file(content: str) -> Path:
    """Create a temporary .env file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".env", prefix="test_env_")
    os.close(fd)
    Path(path).write_text(content, encoding="utf-8")
    return Path(path)


# ── Test: Project root derivation ────────────────────────────────────

class TestProjectRootDerivation:
    """PROJECT_ROOT must be based on __file__, not CWD."""

    def test_project_root_is_absolute(self):
        assert PROJECT_ROOT.is_absolute()

    def test_project_root_contains_main_files(self):
        assert (PROJECT_ROOT / "config_loader.py").is_file()
        assert (PROJECT_ROOT / "stock_watch_list_back_end.py").is_file()

    def test_project_root_cwd_independent(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        # Even from a different CWD, PROJECT_ROOT doesn't change
        assert (PROJECT_ROOT / "config_loader.py").is_file()


# ── Test: .env line parsing ──────────────────────────────────────────

class TestEnvLineParsing:

    def test_simple_key_value(self):
        assert _parse_env_line("FOO=bar") == ("FOO", "bar")

    def test_quoted_double(self):
        assert _parse_env_line('FOO="hello world"') == ("FOO", "hello world")

    def test_quoted_single(self):
        assert _parse_env_line("FOO='hello world'") == ("FOO", "hello world")

    def test_comment_line(self):
        assert _parse_env_line("# This is a comment") is None

    def test_empty_line(self):
        assert _parse_env_line("") is None
        assert _parse_env_line("   ") is None

    def test_no_equals(self):
        assert _parse_env_line("NOT_A_CONFIG") is None

    def test_empty_key(self):
        assert _parse_env_line("=value") is None

    def test_value_with_equals(self):
        assert _parse_env_line("URL=http://example.com?a=b") == ("URL", "http://example.com?a=b")

    def test_whitespace_stripped(self):
        assert _parse_env_line("  FOO  =  bar  ") == ("FOO", "bar")


# ── Test: .env file loading ──────────────────────────────────────────

class TestEnvFileLoading:

    def test_load_explicit_path(self, monkeypatch):
        monkeypatch.delenv("TEST_P2_VAR_A", raising=False)
        env_file = _make_env_file("TEST_P2_VAR_A=hello123\n")
        try:
            load_project_env(env_file)
            assert os.environ.get("TEST_P2_VAR_A") == "hello123"
        finally:
            os.environ.pop("TEST_P2_VAR_A", None)
            env_file.unlink(missing_ok=True)

    def test_load_nonexistent_file(self):
        # Should not raise, just return resolved path
        result = load_project_env(Path("/nonexistent/.env"))
        assert result.name == ".env"
        assert not result.exists()

    def test_load_empty_env_file(self, monkeypatch):
        monkeypatch.delenv("TEST_P2_EMPTY", raising=False)
        env_file = _make_env_file("")
        try:
            load_project_env(env_file)
            assert os.environ.get("TEST_P2_EMPTY") is None
        finally:
            env_file.unlink(missing_ok=True)

    def test_load_env_with_comments(self, monkeypatch):
        monkeypatch.delenv("TEST_P2_C1", raising=False)
        monkeypatch.delenv("TEST_P2_C2", raising=False)
        env_file = _make_env_file("# Comment\nTEST_P2_C1=value1\n\nTEST_P2_C2=value2\n")
        try:
            load_project_env(env_file)
            assert os.environ.get("TEST_P2_C1") == "value1"
            assert os.environ.get("TEST_P2_C2") == "value2"
        finally:
            os.environ.pop("TEST_P2_C1", None)
            os.environ.pop("TEST_P2_C2", None)
            env_file.unlink(missing_ok=True)


# ── Test: Process env priority ───────────────────────────────────────

class TestProcessEnvPriority:
    """Process env vars must NOT be overridden by .env."""

    def test_existing_env_not_overridden(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_OVERRIDE", "from_process")
        env_file = _make_env_file("TEST_P2_OVERRIDE=from_file\n")
        try:
            load_project_env(env_file)
            assert os.environ["TEST_P2_OVERRIDE"] == "from_process"
        finally:
            env_file.unlink(missing_ok=True)

    def test_override_flag_overwrites(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_FORCE", "from_process")
        env_file = _make_env_file("TEST_P2_FORCE=from_file\n")
        try:
            load_project_env(env_file, override=True)
            assert os.environ["TEST_P2_FORCE"] == "from_file"
        finally:
            os.environ.pop("TEST_P2_FORCE", None)
            env_file.unlink(missing_ok=True)

    def test_multiple_vars_mixed_priority(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_EXISTING", "keep_me")
        monkeypatch.delenv("TEST_P2_NEW", raising=False)
        env_file = _make_env_file(
            "TEST_P2_EXISTING=overwrite\nTEST_P2_NEW=loaded\n"
        )
        try:
            load_project_env(env_file)
            assert os.environ["TEST_P2_EXISTING"] == "keep_me"
            assert os.environ["TEST_P2_NEW"] == "loaded"
        finally:
            os.environ.pop("TEST_P2_EXISTING", None)
            os.environ.pop("TEST_P2_NEW", None)
            env_file.unlink(missing_ok=True)


# ── Test: CWD independence ───────────────────────────────────────────

class TestCwdIndependence:
    """Loading must find the same .env regardless of CWD."""

    def test_loads_project_env_from_different_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        # This should still load the project .env (if it exists)
        # or at minimum not crash
        result = load_project_env()
        # Result should point to PROJECT_ROOT / .env
        assert result == PROJECT_ROOT / ".env"

    def test_explicit_path_works_from_any_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TEST_P2_CWD_VAR", raising=False)
        env_file = _make_env_file("TEST_P2_CWD_VAR=found\n")
        try:
            load_project_env(env_file)
            assert os.environ.get("TEST_P2_CWD_VAR") == "found"
        finally:
            os.environ.pop("TEST_P2_CWD_VAR", None)
            env_file.unlink(missing_ok=True)


# ── Test: get_config helpers ─────────────────────────────────────────

class TestGetConfig:

    def test_get_config_existing(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_GET", "value")
        assert get_config("TEST_P2_GET") == "value"

    def test_get_config_default(self):
        assert get_config("TEST_P2_NONEXISTENT_VAR", "fallback") == "fallback"

    def test_get_config_required_missing(self, monkeypatch):
        monkeypatch.delenv("TEST_P2_REQUIRED_MISSING", raising=False)
        with pytest.raises(ConfigError, match="not set"):
            get_config("TEST_P2_REQUIRED_MISSING", required=True)

    def test_get_config_required_placeholder(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_PLACEHOLDER", "your_api_key_here")
        with pytest.raises(ConfigError, match="placeholder"):
            get_config("TEST_P2_PLACEHOLDER", required=True)

    def test_get_config_required_ok(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_OK", "real_value")
        assert get_config("TEST_P2_OK", required=True) == "real_value"

    def test_get_config_int(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_INT", "42")
        assert get_config_int("TEST_P2_INT", 0) == 42

    def test_get_config_int_fallback(self, monkeypatch):
        monkeypatch.delenv("TEST_P2_INT_BAD", raising=False)
        assert get_config_int("TEST_P2_INT_BAD", 99) == 99

    def test_get_config_int_min_value(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_INT_MIN", "1")
        assert get_config_int("TEST_P2_INT_MIN", 5, min_value=3) == 3

    def test_get_config_int_invalid(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_INT_INVALID", "not_a_number")
        assert get_config_int("TEST_P2_INT_INVALID", 7) == 7

    def test_get_config_float(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_FLOAT", "3.14")
        assert get_config_float("TEST_P2_FLOAT", 0.0) == 3.14

    def test_get_config_float_min(self, monkeypatch):
        monkeypatch.setenv("TEST_P2_FLOAT_MIN", "0.1")
        assert get_config_float("TEST_P2_FLOAT_MIN", 1.0, min_value=0.5) == 0.5

    def test_get_config_bool_true(self, monkeypatch):
        for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
            monkeypatch.setenv("TEST_P2_BOOL", val)
            assert get_config_bool("TEST_P2_BOOL") is True

    def test_get_config_bool_false(self, monkeypatch):
        for val in ("0", "false", "no", "off", "FALSE", "No"):
            monkeypatch.setenv("TEST_P2_BOOL", val)
            assert get_config_bool("TEST_P2_BOOL") is False


# ── Test: .env.example completeness ──────────────────────────────────

class TestEnvExampleCompleteness:
    """ .env.example should document all env vars used in the codebase."""

    @pytest.fixture
    def env_example_vars(self) -> set[str]:
        env_path = _PROJECT_ROOT / ".env.example"
        text = env_path.read_text(encoding="utf-8")
        vars_found = set()
        for line in text.splitlines():
            parsed = _parse_env_line(line)
            if parsed:
                vars_found.add(parsed[0])
        return vars_found

    def test_env_example_exists(self):
        assert (_PROJECT_ROOT / ".env.example").is_file()

    def test_env_example_has_smtp_vars(self, env_example_vars):
        for var in ("REPORT_SMTP_HOST", "REPORT_SMTP_PORT", "REPORT_SMTP_USER",
                     "REPORT_SMTP_AUTH_CODE", "REPORT_SMTP_FROM"):
            assert var in env_example_vars, f"{var} missing from .env.example"

    def test_env_example_has_llm_vars(self, env_example_vars):
        for var in ("LLM_PROVIDER", "DASHSCOPE_API_KEY", "QWEN_MODEL"):
            assert var in env_example_vars, f"{var} missing from .env.example"

    def test_env_example_has_backend_vars(self, env_example_vars):
        for var in ("STOCK_API_BASE_URL", "STOCK_DEV_MODE"):
            assert var in env_example_vars, f"{var} missing from .env.example"

    def test_env_example_has_login_vars(self, env_example_vars):
        for var in ("LOGIN_MAX_FAILURES", "LOGIN_LOCKOUT_SECONDS", "LOGIN_WINDOW_SECONDS"):
            assert var in env_example_vars, f"{var} missing from .env.example"

    def test_env_example_has_queue_vars(self, env_example_vars):
        for var in ("REPORT_MAX_GLOBAL_PENDING", "REPORT_MAX_PENDING_PER_USER",
                     "REPORT_MAX_GLOBAL_RUNNING", "REPORT_MAX_QUEUE_HOURS"):
            assert var in env_example_vars, f"{var} missing from .env.example"

    def test_env_example_has_download_vars(self, env_example_vars):
        for var in ("REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER",
                     "REPORT_DOWNLOAD_DAILY_LIMIT_PER_USER",
                     "REPORT_DOWNLOAD_MAX_GLOBAL_ACTIVE"):
            assert var in env_example_vars, f"{var} missing from .env.example"

    def test_env_example_has_article_fetch_vars(self, env_example_vars):
        for var in ("ARTICLE_FETCH_ENABLED", "ARTICLE_FETCH_MAX_URLS",
                     "ARTICLE_FETCH_MAX_REDIRECTS"):
            assert var in env_example_vars, f"{var} missing from .env.example"

    def test_env_example_has_serper_vars(self, env_example_vars):
        for var in ("SERPER_API_KEY", "SERPER_API_BASE", "SERPER_TYPES"):
            assert var in env_example_vars, f"{var} missing from .env.example"

    def test_env_example_has_searxng_vars(self, env_example_vars):
        for var in ("SEARXNG_URL", "SEARXNG_LANGUAGE", "SEARXNG_TIMEOUT"):
            assert var in env_example_vars, f"{var} missing from .env.example"

    def test_env_example_no_real_secrets(self):
        text = (_PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        # Check that values are placeholders, not real keys
        for line in text.splitlines():
            parsed = _parse_env_line(line)
            if parsed is None:
                continue
            key, value = parsed
            if "API_KEY" in key or "AUTH_CODE" in key or "PASSWORD" in key:
                assert value.startswith("your_") or value == "", \
                    f"{key} in .env.example has a non-placeholder value: {value[:20]}..."


# ── Test: All entry points use unified loader ────────────────────────

class TestUnifiedLoaderUsage:
    """All entry points must use config_loader, not dotenv directly."""

    def test_backend_uses_config_loader(self):
        source = (_PROJECT_ROOT / "stock_watch_list_back_end.py").read_text("utf-8")
        assert "from config_loader import" in source
        assert "load_project_env" in source
        # Must NOT use bare load_dotenv() without path
        assert "load_dotenv()" not in source

    def test_worker_uses_config_loader(self):
        source = (_PROJECT_ROOT / "daily_report" / "worker.py").read_text("utf-8")
        assert "from config_loader import" in source
        assert "load_project_env" in source
        assert "from dotenv import" not in source

    def test_config_py_delegates_to_config_loader(self):
        source = (_PROJECT_ROOT / "daily_report" / "src" / "stock_daily_agent" / "config.py").read_text("utf-8")
        assert "config_loader" in source
        assert "load_project_env" in source
        # Should NOT have its own file-parsing logic anymore
        assert "env_path.read_text" not in source

    def test_config_loader_exists(self):
        assert (_PROJECT_ROOT / "config_loader.py").is_file()

    def test_no_bare_load_dotenv_in_product_code(self):
        """No product .py file (excluding tests/) should call bare load_dotenv()."""
        skip_dirs = {"tests", "__pycache__", ".git", "runs", ".venv", "venv"}
        for py_file in _PROJECT_ROOT.rglob("*.py"):
            rel_parts = py_file.relative_to(_PROJECT_ROOT).parts
            if rel_parts[0] in skip_dirs:
                continue
            try:
                text = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            # Allow: from dotenv import load_dotenv  (import is fine if unused)
            # Flag: load_dotenv()  (bare call without path = CWD-dependent)
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "load_dotenv()" in stripped and "def " not in stripped:
                    pytest.fail(
                        f"{py_file.name}:{i} calls bare load_dotenv() — "
                        f"use load_project_env() instead"
                    )


# ── Test: .env not in git ────────────────────────────────────────────

class TestEnvGitignore:

    def test_env_is_gitignored(self):
        gitignore = (_PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gitignore

    def test_env_example_is_tracked(self):
        gitignore = (_PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        # .env.example should NOT be ignored
        lines = gitignore.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped == ".env.example" or stripped == ".env*":
                pytest.fail(f".env.example would be gitignored by rule: {stripped}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
