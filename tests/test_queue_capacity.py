"""Tests for P1-8: queue capacity, expiry, and scheduled task control.

Covers:
- Per-user pending limit (manual + schedule jobs share same limit)
- Global pending limit
- Global running limit
- Manual and schedule jobs use the same limits
- Same schedule occurrence only creates one job (dedup)
- Queue full returns clear QueueFullError (not silent drop)
- Expired jobs are marked correctly
- Normal jobs are processed in order
- Concurrent materialization does not create duplicates
- Concurrent workers do not claim the same job
- Recovery does not breach limits
- P1-3 email dedup status is not regressed

Run with:
    python -m pytest tests/test_queue_capacity.py -v
or:
    python tests/test_queue_capacity.py
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the project root is on sys.path so we can import daily_report.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from daily_report.jobs import (  # noqa: E402
    ACTIVE_STATUSES,
    EXPIRED_STATUS,
    QueueFullError,
    _db_path,
    _INITIALIZED_DATABASES,
    _max_global_pending,
    _max_pending_per_user,
    _max_global_running,
    _max_queue_hours,
    claim_next_job,
    create_weekly_schedule,
    enqueue_email_job,
    expire_stale_queued_jobs,
    get_job,
    init_job_db,
    mark_email_sent,
    mark_job_failure,
    mark_job_sent,
    materialize_due_schedules,
    next_weekly_run_at,
    prune_old_jobs,
    recover_interrupted_jobs,
    store_generated_report,
)
from daily_report.jobs import _connect  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────

def _fresh_db() -> Path:
    """Create a fresh temporary job database and point the module at it."""
    tmpdir = tempfile.mkdtemp(prefix="p18_test_")
    db_file = Path(tmpdir) / "test_jobs.db"
    os.environ["REPORT_JOB_DB"] = str(db_file)
    _INITIALIZED_DATABASES.discard(str(_db_path()))
    init_job_db()
    return db_file


def _cleanup(db_file: Path) -> None:
    _INITIALIZED_DATABASES.discard(str(_db_path()))
    os.environ.pop("REPORT_JOB_DB", None)
    # Clean up env vars that tests might set
    for key in (
        "REPORT_MAX_GLOBAL_PENDING",
        "REPORT_MAX_PENDING_PER_USER",
        "REPORT_MAX_GLOBAL_RUNNING",
        "REPORT_MAX_QUEUE_HOURS",
    ):
        os.environ.pop(key, None)
    try:
        db_file.unlink(missing_ok=True)
        db_file.parent.rmdir()
    except OSError:
        pass


def _make_job(owner: str = "alice", ticker: str = "AAPL") -> str:
    """Enqueue a manual job and return its id."""
    result = enqueue_email_job(
        owner_key=owner,
        ticker=ticker,
        recipient_email=f"{owner}@example.com",
    )
    return result["id"]


def _make_schedule(owner: str = "alice", ticker: str = "AAPL") -> str:
    """Create a weekly schedule and return its id."""
    now = dt.datetime.now(dt.timezone.utc)
    weekday = now.weekday()
    local_time = "09:00"
    result = create_weekly_schedule(
        owner_key=owner,
        ticker=ticker,
        recipient_email=f"{owner}@example.com",
        weekday=weekday,
        local_time=local_time,
    )
    return result["id"]


def _set_schedule_due(schedule_id: str) -> None:
    """Set a schedule's next_run_at to the past so it becomes due."""
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "UPDATE report_schedules SET next_run_at=? WHERE id=?",
            (past, schedule_id),
        )


def _count_jobs_by_status(*statuses: str) -> int:
    with _connect() as conn:
        placeholders = ",".join("?" * len(statuses))
        return conn.execute(
            f"SELECT COUNT(*) FROM report_jobs WHERE status IN ({placeholders})",
            statuses,
        ).fetchone()[0]


def _count_active_jobs(owner_key: str | None = None) -> int:
    with _connect() as conn:
        if owner_key:
            return conn.execute(
                "SELECT COUNT(*) FROM report_jobs WHERE owner_key=? AND status IN (?, ?, ?)",
                (owner_key, *ACTIVE_STATUSES),
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM report_jobs WHERE status IN (?, ?, ?)",
            ACTIVE_STATUSES,
        ).fetchone()[0]


# ── Tests: per-user pending limit ──────────────────────────────────

class TestPerUserPendingLimit:
    def test_manual_job_blocked_when_per_user_limit_reached(self):
        """Enqueueing more than REPORT_MAX_PENDING_PER_USER manual jobs
        for the same user should raise QueueFullError."""
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "2"
        db_file = _fresh_db()
        try:
            _make_job("alice", "AAPL")
            _make_job("alice", "MSFT")
            try:
                _make_job("alice", "GOOG")
                assert False, "Should have raised QueueFullError"
            except QueueFullError as exc:
                assert "2" in str(exc)
        finally:
            _cleanup(db_file)

    def test_different_users_have_independent_limits(self):
        """Each user gets their own per-user pending limit."""
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "2"
        db_file = _fresh_db()
        try:
            _make_job("alice", "AAPL")
            _make_job("alice", "MSFT")
            # Bob should still be able to enqueue
            job_id = _make_job("bob", "TSLA")
            assert job_id is not None
        finally:
            _cleanup(db_file)

    def test_per_user_limit_default_is_5(self):
        """Default per-user pending limit should be 5."""
        db_file = _fresh_db()
        try:
            assert _max_pending_per_user() == 5
        finally:
            _cleanup(db_file)


# ── Tests: global pending limit ────────────────────────────────────

class TestGlobalPendingLimit:
    def test_global_pending_limit_blocks_manual_job(self):
        """When global pending limit is reached, enqueue should fail."""
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "2"
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "10"
        db_file = _fresh_db()
        try:
            _make_job("alice", "AAPL")
            _make_job("bob", "MSFT")
            try:
                _make_job("carol", "GOOG")
                assert False, "Should have raised QueueFullError"
            except QueueFullError as exc:
                assert "queue" in str(exc).lower() or "full" in str(exc).lower()
        finally:
            _cleanup(db_file)

    def test_global_pending_limit_default_is_50(self):
        db_file = _fresh_db()
        try:
            assert _max_global_pending() == 50
        finally:
            _cleanup(db_file)


# ── Tests: global running limit ────────────────────────────────────

class TestGlobalRunningLimit:
    def test_running_limit_prevents_claim(self):
        """When running limit is reached, claim_next_job returns None."""
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "1"
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")
            # Claim the first job — should succeed
            job = claim_next_job()
            assert job is not None
            assert job["id"] == job_id

            # Enqueue another job
            job_id2 = _make_job("bob", "MSFT")
            # Claim should return None because running limit is 1
            job2 = claim_next_job()
            assert job2 is None
        finally:
            _cleanup(db_file)

    def test_running_limit_allows_claim_after_completion(self):
        """After a running job completes, a new claim should succeed."""
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "1"
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")
            job = claim_next_job()
            assert job is not None

            # Complete the job
            mark_job_sent(job_id)

            # New job should be claimable
            job_id2 = _make_job("bob", "MSFT")
            job2 = claim_next_job()
            assert job2 is not None
            assert job2["id"] == job_id2
        finally:
            _cleanup(db_file)

    def test_running_limit_default_is_1(self):
        db_file = _fresh_db()
        try:
            assert _max_global_running() == 1
        finally:
            _cleanup(db_file)


# ── Tests: manual and schedule use same limits ─────────────────────

class TestManualScheduleSameLimits:
    def test_schedule_respects_per_user_pending_limit(self):
        """Schedule materialization should respect per-user pending limit."""
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "2"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        db_file = _fresh_db()
        try:
            # Create 5 schedules for alice, all due now
            for ticker in ["AAPL", "MSFT", "GOOG", "TSLA", "AMZN"]:
                sched = _make_schedule("alice", ticker)
                _set_schedule_due(sched)

            created = materialize_due_schedules()
            # Only 2 should be created (per-user limit)
            assert created == 2
            assert _count_active_jobs("alice") == 2
        finally:
            _cleanup(db_file)

    def test_schedule_respects_global_pending_limit(self):
        """Schedule materialization should respect global pending limit."""
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "3"
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "100"
        db_file = _fresh_db()
        try:
            # Create schedules for multiple users
            for owner in ["alice", "bob", "carol", "dave"]:
                for ticker in ["AAPL", "MSFT"]:
                    sched = _make_schedule(owner, ticker)
                    _set_schedule_due(sched)

            created = materialize_due_schedules()
            # Only 3 should be created (global limit)
            assert created == 3
            assert _count_active_jobs() == 3
        finally:
            _cleanup(db_file)

    def test_manual_and_schedule_share_per_user_limit(self):
        """Manual and schedule jobs count toward the same per-user limit."""
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "3"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        db_file = _fresh_db()
        try:
            # Create 2 manual jobs for alice
            _make_job("alice", "AAPL")
            _make_job("alice", "MSFT")

            # Create 3 schedules for alice, all due
            for ticker in ["GOOG", "TSLA", "AMZN"]:
                sched = _make_schedule("alice", ticker)
                _set_schedule_due(sched)

            created = materialize_due_schedules()
            # Only 1 more can be created (2 manual + 1 schedule = 3 = limit)
            assert created == 1
            assert _count_active_jobs("alice") == 3
        finally:
            _cleanup(db_file)


# ── Tests: occurrence dedup ─────────────────────────────────────────

class TestOccurrenceDedup:
    def test_same_occurrence_not_duplicated(self):
        """Materializing the same due schedule twice should not create
        duplicate jobs."""
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "100"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        db_file = _fresh_db()
        try:
            sched_id = _make_schedule("alice", "AAPL")
            _set_schedule_due(sched_id)

            created1 = materialize_due_schedules()
            assert created1 == 1

            # Reset the schedule to be due again with same occurrence
            with _connect() as conn:
                row = conn.execute(
                    "SELECT last_scheduled_for FROM report_schedules WHERE id=?",
                    (sched_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE report_schedules SET next_run_at=? WHERE id=?",
                    (row["last_scheduled_for"], sched_id),
                )

            created2 = materialize_due_schedules()
            # Should be 0 — same occurrence already has a job
            assert created2 == 0
            assert _count_active_jobs("alice") == 1
        finally:
            _cleanup(db_file)

    def test_concurrent_materialization_no_duplicates(self):
        """Two threads calling materialize_due_schedules simultaneously
        should not create duplicate jobs."""
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "100"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        db_file = _fresh_db()
        try:
            sched_id = _make_schedule("alice", "AAPL")
            _set_schedule_due(sched_id)

            results = []
            errors = []

            def materialize():
                try:
                    count = materialize_due_schedules()
                    results.append(count)
                except Exception as exc:
                    errors.append(exc)

            t1 = threading.Thread(target=materialize)
            t2 = threading.Thread(target=materialize)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            assert len(errors) == 0, f"Unexpected errors: {errors}"
            # Exactly one thread should create the job
            total_created = sum(results)
            assert total_created == 1
            assert _count_active_jobs("alice") == 1
        finally:
            _cleanup(db_file)


# ── Tests: queue full returns clear result ──────────────────────────

class TestQueueFullResponse:
    def test_queue_full_raises_not_silent(self):
        """When queue is full, enqueue_email_job should raise
        QueueFullError, not silently drop the job."""
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "1"
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "10"
        db_file = _fresh_db()
        try:
            _make_job("alice", "AAPL")
            try:
                _make_job("bob", "MSFT")
                assert False, "Should have raised QueueFullError"
            except QueueFullError:
                pass  # Expected
        finally:
            _cleanup(db_file)

    def test_queue_full_error_has_descriptive_message(self):
        """QueueFullError should have a message that tells the user
        what happened."""
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "1"
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "10"
        db_file = _fresh_db()
        try:
            _make_job("alice", "AAPL")
            try:
                _make_job("bob", "MSFT")
            except QueueFullError as exc:
                msg = str(exc)
                assert "1" in msg or "full" in msg.lower()
        finally:
            _cleanup(db_file)


# ── Tests: expired jobs ─────────────────────────────────────────────

class TestExpiredJobs:
    def test_expire_stale_queued_jobs(self):
        """Jobs queued longer than REPORT_MAX_QUEUE_HOURS should be
        marked as expired."""
        os.environ["REPORT_MAX_QUEUE_HOURS"] = "1"
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")

            # Set created_at to 2 hours ago
            old_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(timespec="seconds")
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET created_at=? WHERE id=?",
                    (old_time, job_id),
                )

            expired = expire_stale_queued_jobs()
            assert expired == 1

            job = get_job(job_id)
            assert job["status"] == "expired"
        finally:
            _cleanup(db_file)

    def test_fresh_jobs_not_expired(self):
        """Jobs that are within the max queue time should not be expired."""
        os.environ["REPORT_MAX_QUEUE_HOURS"] = "6"
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")

            expired = expire_stale_queued_jobs()
            assert expired == 0

            job = get_job(job_id)
            assert job["status"] == "queued"
        finally:
            _cleanup(db_file)

    def test_expired_jobs_not_claimed_by_worker(self):
        """Expired jobs should not be claimed by claim_next_job."""
        os.environ["REPORT_MAX_QUEUE_HOURS"] = "1"
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "10"
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")

            old_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(timespec="seconds")
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET created_at=? WHERE id=?",
                    (old_time, job_id),
                )

            expire_stale_queued_jobs()

            job = claim_next_job()
            assert job is None  # No claimable jobs
        finally:
            _cleanup(db_file)

    def test_expired_jobs_pruned(self):
        """Expired jobs should be included in prune_old_jobs cleanup."""
        os.environ["REPORT_MAX_QUEUE_HOURS"] = "1"
        os.environ["REPORT_JOB_RETENTION_DAYS"] = "1"
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")

            # Set created_at to 2 hours ago (triggers expiry)
            old_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(timespec="seconds")
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET created_at=? WHERE id=?",
                    (old_time, job_id),
                )

            expire_stale_queued_jobs()

            # Set finished_at to 2 days ago (beyond retention_days=1)
            very_old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat(timespec="seconds")
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET finished_at=? WHERE id=?",
                    (very_old, job_id),
                )

            pruned = prune_old_jobs()
            assert pruned >= 1

            job = get_job(job_id)
            assert job is None
        finally:
            _cleanup(db_file)

    def test_max_queue_hours_default_is_6(self):
        db_file = _fresh_db()
        try:
            assert _max_queue_hours() == 6
        finally:
            _cleanup(db_file)


# ── Tests: normal jobs processed in order ───────────────────────────

class TestJobOrdering:
    def test_jobs_claimed_in_created_at_order(self):
        """Jobs should be claimed in FIFO order (created_at ASC)."""
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "10"
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "10"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        db_file = _fresh_db()
        try:
            job1 = _make_job("alice", "AAPL")
            # Small delay to ensure different created_at
            import time
            time.sleep(1.1)
            job2 = _make_job("bob", "MSFT")
            time.sleep(1.1)
            job3 = _make_job("carol", "GOOG")

            claimed1 = claim_next_job()
            assert claimed1["id"] == job1
            mark_job_sent(job1)

            claimed2 = claim_next_job()
            assert claimed2["id"] == job2
            mark_job_sent(job2)

            claimed3 = claim_next_job()
            assert claimed3["id"] == job3
        finally:
            _cleanup(db_file)


# ── Tests: concurrent worker no double-claim ───────────────────────

class TestConcurrentWorker:
    def test_concurrent_workers_no_double_claim(self):
        """Two threads calling claim_next_job should not claim the same job."""
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "10"
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "10"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        db_file = _fresh_db()
        try:
            _make_job("alice", "AAPL")

            claimed_ids = []
            lock = threading.Lock()

            def claim():
                job = claim_next_job()
                if job:
                    with lock:
                        claimed_ids.append(job["id"])

            t1 = threading.Thread(target=claim)
            t2 = threading.Thread(target=claim)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            # Exactly one thread should have claimed the job
            assert len(claimed_ids) == 1
        finally:
            _cleanup(db_file)

    def test_concurrent_workers_claim_different_jobs(self):
        """Two workers should claim different jobs when multiple are available."""
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "10"
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "10"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        db_file = _fresh_db()
        try:
            job1 = _make_job("alice", "AAPL")
            import time
            time.sleep(1.1)
            job2 = _make_job("bob", "MSFT")

            claimed = []
            lock = threading.Lock()

            def claim():
                job = claim_next_job()
                if job:
                    with lock:
                        claimed.append(job["id"])

            t1 = threading.Thread(target=claim)
            t2 = threading.Thread(target=claim)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            assert len(claimed) == 2
            assert set(claimed) == {job1, job2}
        finally:
            _cleanup(db_file)


# ── Tests: recovery does not breach limits ──────────────────────────

class TestRecoveryLimits:
    def test_recovery_does_not_breach_running_limit(self):
        """After recovery, jobs go back to 'queued' (not 'generating'),
        so running limit is not affected."""
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "1"
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")

            # Simulate crash: set to 'generating'
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET status='generating' WHERE id=?",
                    (job_id,),
                )

            # Recovery should put it back to 'queued'
            recovered = recover_interrupted_jobs()
            assert recovered == 1

            # Running count should be 0
            with _connect() as conn:
                running = conn.execute(
                    "SELECT COUNT(*) FROM report_jobs WHERE status IN ('generating', 'sending')"
                ).fetchone()[0]
            assert running == 0

            # Should be able to claim
            job = claim_next_job()
            assert job is not None
            assert job["id"] == job_id
        finally:
            _cleanup(db_file)

    def test_recovery_multiple_jobs_all_queued(self):
        """Multiple interrupted jobs should all go back to queued."""
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "1"
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "10"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        db_file = _fresh_db()
        try:
            job1 = _make_job("alice", "AAPL")
            job2 = _make_job("bob", "MSFT")

            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET status='generating' WHERE id IN (?, ?)",
                    (job1, job2),
                )

            recovered = recover_interrupted_jobs()
            assert recovered == 2

            # All should be queued
            assert _count_jobs_by_status("queued") == 2
            assert _count_jobs_by_status("generating", "sending") == 0
        finally:
            _cleanup(db_file)


# ── Tests: schedule materialization with partial capacity ──────────

class TestScheduleMaterializationPartial:
    def test_skipped_schedule_retried_next_cycle(self):
        """When per-user limit blocks a schedule, it should NOT advance
        next_run_at, so it retries in the next cycle."""
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "1"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        db_file = _fresh_db()
        try:
            # Create 2 schedules for alice
            sched1 = _make_schedule("alice", "AAPL")
            sched2 = _make_schedule("alice", "MSFT")
            _set_schedule_due(sched1)
            _set_schedule_due(sched2)

            created = materialize_due_schedules()
            # Only 1 should be created (per-user limit = 1)
            assert created == 1
            assert _count_active_jobs("alice") == 1

            # One schedule should still be due (next_run_at in the past)
            with _connect() as conn:
                due = conn.execute(
                    "SELECT COUNT(*) FROM report_schedules "
                    "WHERE is_active=1 AND next_run_at<=?",
                    (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),),
                ).fetchone()[0]
            assert due == 1
        finally:
            _cleanup(db_file)

    def test_skipped_schedule_materialized_after_capacity_frees(self):
        """After a job completes, a previously skipped schedule should
        be materialized in the next cycle."""
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "1"
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "100"
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "10"
        db_file = _fresh_db()
        try:
            sched1 = _make_schedule("alice", "AAPL")
            sched2 = _make_schedule("alice", "MSFT")
            _set_schedule_due(sched1)
            _set_schedule_due(sched2)

            created1 = materialize_due_schedules()
            assert created1 == 1

            # Complete the first job
            job = claim_next_job()
            assert job is not None
            mark_job_sent(job["id"])

            # Now materialize again — the skipped schedule should be picked up
            created2 = materialize_due_schedules()
            assert created2 == 1
            assert _count_active_jobs("alice") == 1
        finally:
            _cleanup(db_file)


# ── Tests: P1-3 email dedup no regression ───────────────────────────

class TestP1DedupNoRegression:
    def test_email_sent_at_preserved_through_expiry(self):
        """expire_stale_queued_jobs should not touch email_sent_at
        on jobs that already have it set."""
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")
            msg_id = f"<job-{job_id}@example.com>"

            # Set email_sent_at (simulating P1-3 crash recovery state)
            mark_email_sent(job_id, msg_id)

            # Set job to 'queued' with old created_at
            old_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=10)).isoformat(timespec="seconds")
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET created_at=?, status='queued' WHERE id=?",
                    (old_time, job_id),
                )

            os.environ["REPORT_MAX_QUEUE_HOURS"] = "1"
            expire_stale_queued_jobs()

            # email_sent_at should still be set
            with _connect() as conn:
                row = conn.execute(
                    "SELECT email_sent_at, smtp_message_id FROM report_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
            assert row["email_sent_at"] is not None
            assert row["smtp_message_id"] == msg_id
        finally:
            _cleanup(db_file)

    def test_mark_job_sent_clears_fields_after_expiry_not_needed(self):
        """mark_job_sent should still work correctly for normal flow
        (not affected by P1-8 changes)."""
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "10"
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")
            job = claim_next_job()
            assert job is not None
            mark_job_sent(job_id)

            job = get_job(job_id)
            assert job["status"] == "sent"
        finally:
            _cleanup(db_file)

    def test_public_job_includes_expired_status(self):
        """_public_job should return whatever status is in the row,
        including 'expired'."""
        db_file = _fresh_db()
        try:
            job_id = _make_job("alice", "AAPL")

            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET status='expired' WHERE id=?",
                    (job_id,),
                )

            job = get_job(job_id)
            assert job["status"] == "expired"
        finally:
            _cleanup(db_file)


# ── Tests: env var configuration ───────────────────────────────────

class TestEnvVarConfig:
    def test_config_values_respect_env_vars(self):
        """All config helpers should read from environment variables."""
        os.environ["REPORT_MAX_GLOBAL_PENDING"] = "42"
        os.environ["REPORT_MAX_PENDING_PER_USER"] = "7"
        os.environ["REPORT_MAX_GLOBAL_RUNNING"] = "3"
        os.environ["REPORT_MAX_QUEUE_HOURS"] = "12"

        try:
            assert _max_global_pending() == 42
            assert _max_pending_per_user() == 7
            assert _max_global_running() == 3
            assert _max_queue_hours() == 12
        finally:
            for key in (
                "REPORT_MAX_GLOBAL_PENDING",
                "REPORT_MAX_PENDING_PER_USER",
                "REPORT_MAX_GLOBAL_RUNNING",
                "REPORT_MAX_QUEUE_HOURS",
            ):
                os.environ.pop(key, None)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
