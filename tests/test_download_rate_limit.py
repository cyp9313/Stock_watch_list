"""Tests for P0-3: download generation rate-limiting in jobs.py.

Run with:
    python -m pytest tests/test_download_rate_limit.py -v
or:
    python tests/test_download_rate_limit.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

# Ensure the project root is on sys.path so we can import daily_report.jobs.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from daily_report.jobs import (  # noqa: E402
    ActiveJobError,
    DailyLimitError,
    JobQueueError,
    cleanup_stale_download_generations,
    check_download_generation_limits,
    finish_download_generation,
    start_download_generation,
    init_job_db,
    _db_path,
    _INITIALIZED_DATABASES,
)


def _fresh_db(monkey_env: dict[str, str] | None = None) -> Path:
    """Create a fresh temporary job database and point the module at it."""
    tmpdir = tempfile.mkdtemp(prefix="p0_test_")
    db_file = Path(tmpdir) / "test_jobs.db"
    os.environ["REPORT_JOB_DB"] = str(db_file)
    # Force re-initialization with the new path.
    _INITIALIZED_DATABASES.discard(str(_db_path()))
    init_job_db()
    # Apply env overrides for limits.
    if monkey_env:
        for key, val in monkey_env.items():
            os.environ[key] = val
    return db_file


def _reset_env(keys: list[str]) -> None:
    for key in keys:
        os.environ.pop(key, None)


def _cleanup(db_file: Path) -> None:
    """Remove the temporary database file."""
    db_path_str = str(db_file)
    _INITIALIZED_DATABASES.discard(str(_db_path()))
    try:
        db_file.unlink(missing_ok=True)
    except OSError:
        pass
    # Clean up WAL/SHM files.
    for suffix in ("-wal", "-shm"):
        try:
            (db_file.parent / (db_file.name + suffix)).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        db_file.parent.rmdir()
    except OSError:
        pass


# ─── Tests ────────────────────────────────────────────────────────

def test_guest_blocked() -> None:
    """A guest (empty owner_key) cannot start a download."""
    db = _fresh_db()
    try:
        try:
            check_download_generation_limits("")
            assert False, "Should have raised ValueError for empty owner_key"
        except ValueError:
            pass  # Expected

        try:
            start_download_generation("", "AAPL")
            assert False, "Should have raised ValueError for empty owner_key"
        except ValueError:
            pass  # Expected
    finally:
        _cleanup(db)


def test_logged_in_user_allowed() -> None:
    """A logged-in user with no prior activity can start a download."""
    db = _fresh_db()
    try:
        # Should not raise.
        check_download_generation_limits("user_alice")
        session_id = start_download_generation("user_alice", "AAPL")
        assert session_id, "Session ID should not be empty"
        assert len(session_id) == 32, "Session ID should be a UUID hex string"
    finally:
        _cleanup(db)


def test_per_user_active_limit() -> None:
    """A user with an active download cannot start another."""
    db = _fresh_db({"REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER": "1"})
    try:
        check_download_generation_limits("user_bob")
        start_download_generation("user_bob", "MSFT")

        # Second attempt should be blocked.
        try:
            check_download_generation_limits("user_bob")
            assert False, "Should have raised ActiveJobError"
        except ActiveJobError:
            pass  # Expected
    finally:
        _reset_env(["REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER"])
        _cleanup(db)


def test_different_users_independent() -> None:
    """Two different users can each have their own active download (if global allows)."""
    db = _fresh_db({"REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER": "1",
                    "REPORT_DOWNLOAD_MAX_GLOBAL_ACTIVE": "3"})
    try:
        check_download_generation_limits("user_carol")
        start_download_generation("user_carol", "AAPL")

        # Different user should still be allowed.
        check_download_generation_limits("user_dave")
        start_download_generation("user_dave", "GOOGL")
    finally:
        _reset_env(["REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER",
                     "REPORT_DOWNLOAD_MAX_GLOBAL_ACTIVE"])
        _cleanup(db)


def test_daily_limit() -> None:
    """Daily limit is enforced after the configured number of downloads."""
    db = _fresh_db({"REPORT_DOWNLOAD_DAILY_LIMIT_PER_USER": "2",
                    "REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER": "1"})
    try:
        # First download of the day.
        check_download_generation_limits("user_eve")
        sid1 = start_download_generation("user_eve", "AAPL")
        finish_download_generation(sid1, success=True)

        # Second download of the day.
        check_download_generation_limits("user_eve")
        sid2 = start_download_generation("user_eve", "MSFT")
        finish_download_generation(sid2, success=True)

        # Third download should be blocked by daily limit.
        try:
            check_download_generation_limits("user_eve")
            assert False, "Should have raised DailyLimitError"
        except DailyLimitError:
            pass  # Expected
    finally:
        _reset_env(["REPORT_DOWNLOAD_DAILY_LIMIT_PER_USER",
                     "REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER"])
        _cleanup(db)


def test_global_concurrency_limit() -> None:
    """Global concurrency cap prevents too many simultaneous downloads."""
    db = _fresh_db({"REPORT_DOWNLOAD_MAX_GLOBAL_ACTIVE": "2",
                    "REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER": "1"})
    try:
        # User 1 starts a download.
        check_download_generation_limits("user_frank")
        start_download_generation("user_frank", "AAPL")

        # User 2 starts a download.
        check_download_generation_limits("user_grace")
        start_download_generation("user_grace", "MSFT")

        # User 3 should be blocked by global limit.
        try:
            check_download_generation_limits("user_heidi")
            assert False, "Should have raised ActiveJobError for global limit"
        except ActiveJobError as exc:
            assert "maximum number of reports" in str(exc), \
                f"Error message should mention global limit, got: {exc}"
    finally:
        _reset_env(["REPORT_DOWNLOAD_MAX_GLOBAL_ACTIVE",
                     "REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER"])
        _cleanup(db)


def test_finish_releases_active_slot() -> None:
    """Finishing a download frees the user's active slot."""
    db = _fresh_db({"REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER": "1"})
    try:
        check_download_generation_limits("user_ivan")
        sid = start_download_generation("user_ivan", "AAPL")
        finish_download_generation(sid, success=True)

        # Should be able to start another.
        check_download_generation_limits("user_ivan")
        sid2 = start_download_generation("user_ivan", "MSFT")
        assert sid2 != sid, "New session should have a different ID"
    finally:
        _reset_env(["REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER"])
        _cleanup(db)


def test_finish_failed_releases_active_slot() -> None:
    """Finishing a download as failed also frees the user's active slot."""
    db = _fresh_db({"REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER": "1"})
    try:
        check_download_generation_limits("user_judy")
        sid = start_download_generation("user_judy", "AAPL")
        finish_download_generation(sid, success=False)

        # Should be able to start another.
        check_download_generation_limits("user_judy")
        start_download_generation("user_judy", "MSFT")
    finally:
        _reset_env(["REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER"])
        _cleanup(db)


def test_stale_session_cleanup() -> None:
    """Stale 'generating' sessions are cleaned up after the timeout."""
    db = _fresh_db()
    try:
        # Start a download session.
        check_download_generation_limits("user_mallory")
        sid = start_download_generation("user_mallory", "AAPL")

        # Simulate the session being stale by running cleanup with a future time
        # far enough ahead that the session is considered stale.
        from daily_report.jobs import _DOWNLOAD_STALE_SECONDS
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=_DOWNLOAD_STALE_SECONDS + 60)
        cleaned = cleanup_stale_download_generations(now=future)
        assert cleaned >= 1, "Should have cleaned up at least one stale session"

        # After cleanup, the user should be able to start a new download.
        check_download_generation_limits("user_mallory")
        start_download_generation("user_mallory", "MSFT")
    finally:
        _cleanup(db)


def test_finish_records_elapsed() -> None:
    """finish_download_generation records an elapsed time."""
    db = _fresh_db()
    try:
        import sqlite3
        from daily_report.jobs import _connect

        check_download_generation_limits("user_oscar")
        sid = start_download_generation("user_oscar", "AAPL")
        finish_download_generation(sid, success=True)

        conn = _connect()
        try:
            row = conn.execute(
                "SELECT status, elapsed, finished_at FROM download_generations WHERE id=?",
                (sid,),
            ).fetchone()
            assert row is not None, "Session should exist"
            assert row["status"] == "done", f"Status should be 'done', got '{row['status']}'"
            assert row["elapsed"] is not None, "Elapsed should be recorded"
            assert row["elapsed"] >= 0, "Elapsed should be non-negative"
            assert row["finished_at"] is not None, "Finished_at should be recorded"
        finally:
            conn.close()
    finally:
        _cleanup(db)


# ─── Runner ───────────────────────────────────────────────────────

def _run_all() -> tuple[int, int]:
    """Run all test functions and return (passed, failed) counts."""
    tests = [
        name for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = 0
    failed = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {name}: {exc}")
            failed += 1
    return passed, failed


if __name__ == "__main__":
    print("Running P0-3 download rate-limit tests...\n")
    passed, failed = _run_all()
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed:
        sys.exit(1)
    else:
        print("All tests passed!")
