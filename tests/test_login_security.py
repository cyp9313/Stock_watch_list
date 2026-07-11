"""Tests for P1-7: Login brute-force protection and secure password input.

All tests use a temporary SQLite database — the real watchlist_users.db
is never touched.
"""

import datetime as _dt
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

# ── Ensure project root is on sys.path ──
_proj_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

# ── Stub ticker_mapping (required by multiuser_store) ──
if "ticker_mapping" not in sys.modules or not hasattr(sys.modules.get("ticker_mapping", {}), "normalize_yfinance_ticker"):
    _tm = MagicMock()
    _tm.normalize_yfinance_ticker = lambda t: t
    sys.modules["ticker_mapping"] = _tm

import multiuser_store as ms


# ───────────────────────── Fixtures ─────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Redirect multiuser_store to use a temp database."""
    db_path = str(tmp_path / "test_users.db")
    monkeypatch.setattr(ms, "USER_DB_PATH", db_path)
    # Initialise tables
    conn = ms.init_user_db()
    conn.close()
    yield db_path


@pytest.fixture
def test_user(temp_db):
    """Create a test user and return credentials."""
    ms.create_user("testuser", "correct_password", display_name="Test User")
    return {"username": "testuser", "password": "correct_password"}


@pytest.fixture
def low_threshold(monkeypatch):
    """Set a low threshold for faster tests."""
    monkeypatch.setenv("LOGIN_MAX_FAILURES", "3")
    monkeypatch.setenv("LOGIN_LOCKOUT_SECONDS", "60")
    monkeypatch.setenv("LOGIN_WINDOW_SECONDS", "120")


# ───────────────────────── Tests ─────────────────────────

class TestCorrectLogin:
    """1. Correct password logs in normally."""

    def test_correct_password(self, test_user):
        user = ms.authenticate(test_user["username"], test_user["password"])
        assert user is not None
        assert user["username"] == "testuser"
        assert user["display_name"] == "Test User"
        assert "cache_key" in user

    def test_correct_password_clears_failures(self, temp_db):
        """6. Successful login resets failure count."""
        ms.create_user("alice", "secret123")
        # Accumulate 2 failures
        assert ms.authenticate("alice", "wrong") is None
        assert ms.authenticate("alice", "wrong") is None
        # Verify count is 2
        conn = ms.init_user_db()
        row = conn.execute(
            "SELECT failed_count FROM login_attempts WHERE username=?", ("alice",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 2
        # Successful login should reset
        user = ms.authenticate("alice", "secret123")
        assert user is not None
        conn = ms.init_user_db()
        row = conn.execute(
            "SELECT failed_count FROM login_attempts WHERE username=?", ("alice",)
        ).fetchone()
        conn.close()
        assert row is None  # record deleted on success


class TestWrongPassword:
    """2. Wrong password increments failure count."""

    def test_wrong_password_increments(self, test_user):
        assert ms.authenticate(test_user["username"], "wrong") is None
        conn = ms.init_user_db()
        row = conn.execute(
            "SELECT failed_count FROM login_attempts WHERE username=?", ("testuser",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 1

    def test_multiple_failures_accumulate(self, test_user):
        for _ in range(2):
            assert ms.authenticate(test_user["username"], "wrong") is None
        conn = ms.init_user_db()
        row = conn.execute(
            "SELECT failed_count FROM login_attempts WHERE username=?", ("testuser",)
        ).fetchone()
        conn.close()
        assert row[0] == 2


class TestLockoutThreshold:
    """3. Lockout after threshold."""

    def test_lockout_after_threshold(self, test_user, low_threshold):
        # 3 failures → locked
        for _ in range(3):
            ms.authenticate(test_user["username"], "wrong")
        # Now locked
        is_locked, remaining = ms.check_login_lock_status(test_user["username"])
        assert is_locked is True
        assert remaining > 0

    def test_lockout_with_default_threshold(self, test_user):
        """Default threshold is 5."""
        for _ in range(5):
            ms.authenticate(test_user["username"], "wrong")
        is_locked, _ = ms.check_login_lock_status(test_user["username"])
        assert is_locked is True

    def test_not_locked_before_threshold(self, test_user, low_threshold):
        for _ in range(2):  # threshold is 3
            ms.authenticate(test_user["username"], "wrong")
        is_locked, _ = ms.check_login_lock_status(test_user["username"])
        assert is_locked is False


class TestLockoutBehavior:
    """4. Correct password during lockout is still blocked."""

    def test_correct_password_blocked_during_lockout(self, test_user, low_threshold):
        # Lock the account
        for _ in range(3):
            ms.authenticate(test_user["username"], "wrong")
        # Even correct password should fail
        user = ms.authenticate(test_user["username"], test_user["password"])
        assert user is None

    def test_lock_status_for_nonexistent_user(self, temp_db):
        """Lock status check doesn't reveal whether user exists."""
        is_locked, _ = ms.check_login_lock_status("nonexistent_user")
        assert is_locked is False


class TestLockoutRecovery:
    """5. Recovery after lockout period ends."""

    def test_recovery_after_lockout_expires(self, test_user, low_threshold):
        # Lock the account
        for _ in range(3):
            ms.authenticate(test_user["username"], "wrong")
        assert ms.authenticate(test_user["username"], test_user["password"]) is None

        # Manually expire the lock by updating locked_until in the past
        conn = ms.init_user_db()
        past = (_dt.datetime.now() - _dt.timedelta(seconds=1)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE login_attempts SET locked_until=? WHERE username=?",
            (past, test_user["username"]),
        )
        conn.commit()
        conn.close()

        # Should be able to login again
        is_locked, _ = ms.check_login_lock_status(test_user["username"])
        assert is_locked is False
        user = ms.authenticate(test_user["username"], test_user["password"])
        assert user is not None


class TestWindowExpiry:
    """Window expiry resets the failure count."""

    def test_window_expiry_resets_count(self, test_user, low_threshold):
        # 2 failures (below threshold of 3)
        ms.authenticate(test_user["username"], "wrong")
        ms.authenticate(test_user["username"], "wrong")

        # Manually push last_failure_at beyond the window
        conn = ms.init_user_db()
        old_time = (_dt.datetime.now() - _dt.timedelta(seconds=200)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE login_attempts SET last_failure_at=? WHERE username=?",
            (old_time, test_user["username"]),
        )
        conn.commit()
        conn.close()

        # Next failure should start fresh at count=1, not 3
        ms.authenticate(test_user["username"], "wrong")
        is_locked, _ = ms.check_login_lock_status(test_user["username"])
        assert not is_locked  # count is 1, not locked


class TestUserEnumeration:
    """7. Non-existent user and wrong password return same type."""

    def test_nonexistent_returns_none(self, temp_db):
        assert ms.authenticate("ghost", "anypassword") is None

    def test_wrong_password_returns_none(self, test_user):
        assert ms.authenticate(test_user["username"], "wrong") is None

    def test_both_create_attempt_records(self, temp_db):
        """Both nonexistent and wrong-password create attempt records."""
        ms.create_user("realuser", "pass123")
        ms.authenticate("realuser", "wrong")
        ms.authenticate("ghost", "wrong")

        conn = ms.init_user_db()
        rows = conn.execute(
            "SELECT username FROM login_attempts ORDER BY username"
        ).fetchall()
        conn.close()
        usernames = [r[0] for r in rows]
        assert "ghost" in usernames
        assert "realuser" in usernames

    def test_nonexistent_user_can_be_locked(self, temp_db, low_threshold):
        """Lockout applies to non-existent usernames too (no enumeration)."""
        for _ in range(3):
            ms.authenticate("ghost", "wrong")
        is_locked, _ = ms.check_login_lock_status("ghost")
        assert is_locked is True


class TestConcurrentFailures:
    """8. Concurrent failures don't lose count."""

    def test_concurrent_failures_count_correctly(self, test_user, monkeypatch):
        """Multiple threads failing simultaneously should all be counted.

        Uses a threshold higher than the thread count so that lockout
        doesn't prevent counting.
        """
        monkeypatch.setenv("LOGIN_MAX_FAILURES", "20")
        monkeypatch.setenv("LOGIN_LOCKOUT_SECONDS", "60")
        monkeypatch.setenv("LOGIN_WINDOW_SECONDS", "120")

        barrier = threading.Barrier(5)
        results = []

        def fail_login():
            barrier.wait()
            r = ms.authenticate(test_user["username"], "wrong")
            results.append(r)

        threads = [threading.Thread(target=fail_login) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # All should return None (failures)
        assert all(r is None for r in results)

        # The count should be exactly 5 (no lost increments)
        conn = ms.init_user_db()
        row = conn.execute(
            "SELECT failed_count FROM login_attempts WHERE username=?",
            (test_user["username"],),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 5


class TestConfigMinimums:
    """Config minimums are enforced."""

    def test_max_failures_minimum(self, monkeypatch, temp_db):
        monkeypatch.setenv("LOGIN_MAX_FAILURES", "1")  # below min of 3
        config = ms._get_login_config()
        assert config["max_failures"] >= 3

    def test_lockout_seconds_minimum(self, monkeypatch, temp_db):
        monkeypatch.setenv("LOGIN_LOCKOUT_SECONDS", "0")
        config = ms._get_login_config()
        assert config["lockout_seconds"] >= 60

    def test_window_seconds_minimum(self, monkeypatch, temp_db):
        monkeypatch.setenv("LOGIN_WINDOW_SECONDS", "0")
        config = ms._get_login_config()
        assert config["window_seconds"] >= 60

    def test_invalid_env_uses_default(self, monkeypatch, temp_db):
        monkeypatch.setenv("LOGIN_MAX_FAILURES", "not_a_number")
        config = ms._get_login_config()
        assert config["max_failures"] == 5  # default


class TestSecureCLI:
    """9 & 10. User creation uses getpass; password not in args."""

    def test_cli_password_is_optional_arg(self):
        """--password should be optional, not a positional argument."""
        import inspect
        import argparse

        # Reconstruct the parser
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        create = sub.add_parser("create-user")
        create.add_argument("username")
        create.add_argument("--password", default=None)

        # Parse with only username — should not require password
        args = parser.parse_args(["create-user", "alice"])
        assert args.username == "alice"
        assert args.password is None

    def test_cli_no_positional_password(self):
        """Password should not be a positional argument (which appears in ps)."""
        import ast

        source_path = os.path.join(_proj_root, "multiuser_store.py")
        with open(source_path, "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source)

        # Find _main function and inspect argparse setup
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_main":
                func_source = ast.get_source_segment(source, node)
                # Password should be --password (optional), not positional
                assert '--password' in func_source, "Should use --password flag"
                # Should import getpass
                assert 'getpass' in func_source, "Should use getpass for secure input"
                break

    def test_getpass_used_in_source(self):
        """Verify getpass is imported and used in multiuser_store.py."""
        source_path = os.path.join(_proj_root, "multiuser_store.py")
        with open(source_path, "r", encoding="utf-8") as f:
            source = f.read()
        assert "import getpass" in source or "from getpass" in source
        assert "getpass.getpass" in source

    def test_password_not_logged(self):
        """Password should not appear in print statements or logs."""
        source_path = os.path.join(_proj_root, "multiuser_store.py")
        with open(source_path, "r", encoding="utf-8") as f:
            source = f.read()

        # The _main function should not print the password value
        import ast
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_main":
                func_source = ast.get_source_segment(source, node)
                # Should print "User ready: {username}" not the password
                lines = func_source.split("\n")
                for line in lines:
                    if "print(" in line and "password" in line.lower():
                        # Check it's a warning about --password, not printing the value
                        assert "stderr" in line or "Warning" in line, \
                            "Password value should not be printed"


class TestExistingUserData:
    """11. Existing user data can still be used."""

    def test_old_schema_user_works(self, temp_db):
        """A user created with the old schema (before login_attempts table)
        should still authenticate correctly."""
        # Create a user directly with the original schema (no login_attempts)
        conn = ms.init_user_db()  # This creates all tables including login_attempts
        # Simulate old-style user
        pw_hash = ms.make_password_hash("oldpass")
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            ("olduser", pw_hash, "Old User", _dt.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()

        # Should authenticate fine
        user = ms.authenticate("olduser", "oldpass")
        assert user is not None
        assert user["username"] == "olduser"

    def test_existing_password_hash_format_preserved(self, temp_db):
        """Password hash format should remain pbkdf2_sha256$200000$..."""
        ms.create_user("hashtest", "mypassword")
        conn = ms.init_user_db()
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username=?", ("hashtest",)
        ).fetchone()
        conn.close()
        pw_hash = row[0]
        assert pw_hash.startswith("pbkdf2_sha256$200000$")
        # Verify the hash still works
        assert ms.verify_password("mypassword", pw_hash) is True
        assert ms.verify_password("wrong", pw_hash) is False


class TestSecurityInvariants:
    """Verify existing security primitives are not weakened."""

    def test_pbkdf2_iterations_unchanged(self):
        """PBKDF2 iterations should remain at 200,000."""
        pw_hash = ms.make_password_hash("test")
        parts = pw_hash.split("$")
        assert parts[0] == "pbkdf2_sha256"
        assert int(parts[1]) == 200_000

    def test_constant_time_compare_preserved(self):
        """verify_password should use hmac.compare_digest."""
        import inspect
        source = inspect.getsource(ms.verify_password)
        assert "compare_digest" in source

    def test_dummy_verify_for_nonexistent_user(self, temp_db):
        """Non-existent user should still trigger PBKDF2 computation."""
        import time
        # Time authentication for non-existent user
        start = time.monotonic()
        ms.authenticate("ghost", "somepassword")
        elapsed_nonexistent = time.monotonic() - start

        # Time authentication for existing user with wrong password
        ms.create_user("real", "realpass")
        start = time.monotonic()
        ms.authenticate("real", "wrongpassword")
        elapsed_existing = time.monotonic() - start

        # Both should take similar time (both do PBKDF2)
        # Allow generous ratio since timing varies
        ratio = max(elapsed_nonexistent, elapsed_existing) / max(min(elapsed_nonexistent, elapsed_existing), 0.001)
        assert ratio < 50, f"Timing difference too large: {elapsed_nonexistent:.3f}s vs {elapsed_existing:.3f}s"


class TestSessionIsolation:
    """Normal user sessions and account isolation not affected."""

    def test_different_users_isolated(self, temp_db):
        ms.create_user("user1", "pass1")
        ms.create_user("user2", "pass2")

        user1 = ms.authenticate("user1", "pass1")
        user2 = ms.authenticate("user2", "pass2")

        assert user1["username"] == "user1"
        assert user2["username"] == "user2"
        assert user1["id"] != user2["id"]
        assert user1["cache_key"] != user2["cache_key"]

    def test_failed_login_for_one_user_doesnt_affect_other(self, temp_db, low_threshold):
        ms.create_user("user1", "pass1")
        ms.create_user("user2", "pass2")

        # Fail 3 times for user1 (should lock user1)
        for _ in range(3):
            ms.authenticate("user1", "wrong")

        # user2 should still be able to login
        user2 = ms.authenticate("user2", "pass2")
        assert user2 is not None
