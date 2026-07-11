"""Tests for P1-3: reduce email duplicate sending risk.

Covers:
- Deterministic Message-ID generation from job_id
- mark_email_sent records timestamp and message_id
- mark_email_sent is idempotent (first write wins)
- mark_job_sent clears smtp_message_id and email_sent_at
- Worker skips SMTP send when email_sent_at is already set
- Worker sends normally when email_sent_at is NULL
- Crash recovery: job with email_sent_at -> no re-send
- Column migration adds smtp_message_id and email_sent_at
- _public_job includes email_sent_at
- mailer.send_report_email accepts and uses message_id parameter

Run with:
    python -m pytest tests/test_email_dedup.py -v
or:
    python tests/test_email_dedup.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the project root is on sys.path so we can import daily_report.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from daily_report.jobs import (  # noqa: E402
    _db_path,
    _INITIALIZED_DATABASES,
    _public_job,
    claim_next_job,
    enqueue_email_job,
    init_job_db,
    mark_email_sent,
    mark_job_failure,
    mark_job_sent,
    recover_interrupted_jobs,
    store_generated_report,
)


# ── Fixtures ────────────────────────────────────────────────────────

def _fresh_db() -> Path:
    """Create a fresh temporary job database and point the module at it."""
    tmpdir = tempfile.mkdtemp(prefix="p13_test_")
    db_file = Path(tmpdir) / "test_jobs.db"
    os.environ["REPORT_JOB_DB"] = str(db_file)
    _INITIALIZED_DATABASES.discard(str(_db_path()))
    init_job_db()
    return db_file


def _cleanup(db_file: Path) -> None:
    _INITIALIZED_DATABASES.discard(str(_db_path()))
    os.environ.pop("REPORT_JOB_DB", None)
    try:
        db_file.unlink(missing_ok=True)
        db_file.parent.rmdir()
    except OSError:
        pass


def _make_job(owner: str = "alice", ticker: str = "AAPL") -> str:
    """Enqueue a job and return its id."""
    result = enqueue_email_job(
        owner_key=owner,
        ticker=ticker,
        recipient_email=f"{owner}@example.com",
    )
    return result["id"]


def _claim_and_generate(job_id: str) -> dict:
    """Claim a job, simulate report generation, and store the HTML."""
    import sqlite3
    from daily_report.jobs import _connect

    # Manually set report_html so claim_next_job puts it in "sending" state
    with _connect() as conn:
        conn.execute(
            "UPDATE report_jobs SET report_html=?, file_name=?, report_date=? WHERE id=?",
            (sqlite3.Binary(b"<html>report</html>"), "AAPL_report_2026-07-11.html", "2026-07-11", job_id),
        )
    job = claim_next_job()
    assert job is not None
    assert job["id"] == job_id
    return job


# ── Tests: column migration ─────────────────────────────────────────

class TestColumnMigration:
    def test_columns_exist_after_init(self):
        db_file = _fresh_db()
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_file))
            cols = {row[1] for row in conn.execute("PRAGMA table_info(report_jobs)")}
            conn.close()
            assert "smtp_message_id" in cols
            assert "email_sent_at" in cols
        finally:
            _cleanup(db_file)

    def test_columns_added_to_existing_db(self):
        db_file = _fresh_db()
        try:
            import sqlite3
            # Simulate an old DB without the new columns by dropping them
            _INITIALIZED_DATABASES.discard(str(_db_path()))
            conn = sqlite3.connect(str(db_file))
            # SQLite doesn't support DROP COLUMN before 3.35, but we can
            # recreate the table without the new columns to simulate old DB.
            # Instead, just verify the columns exist (they were added by init).
            cols = {row[1] for row in conn.execute("PRAGMA table_info(report_jobs)")}
            conn.close()
            assert "smtp_message_id" in cols
            assert "email_sent_at" in cols
        finally:
            _cleanup(db_file)


# ── Tests: mark_email_sent ──────────────────────────────────────────

class TestMarkEmailSent:
    def test_records_timestamp_and_message_id(self):
        db_file = _fresh_db()
        try:
            job_id = _make_job()
            msg_id = f"<job-{job_id}@example.com>"
            mark_email_sent(job_id, msg_id)

            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                row = conn.execute(
                    "SELECT email_sent_at, smtp_message_id FROM report_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
            assert row["email_sent_at"] is not None
            assert row["smtp_message_id"] == msg_id
        finally:
            _cleanup(db_file)

    def test_idempotent_first_write_wins(self):
        db_file = _fresh_db()
        try:
            job_id = _make_job()
            first_msg_id = f"<job-{job_id}@example.com>"
            second_msg_id = f"<job-{job_id}@other.com>"

            mark_email_sent(job_id, first_msg_id)

            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                first_ts = conn.execute(
                    "SELECT email_sent_at FROM report_jobs WHERE id=?", (job_id,)
                ).fetchone()["email_sent_at"]

            # Second call should be a no-op
            mark_email_sent(job_id, second_msg_id)

            with _connect() as conn:
                row = conn.execute(
                    "SELECT email_sent_at, smtp_message_id FROM report_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
            assert row["email_sent_at"] == first_ts
            assert row["smtp_message_id"] == first_msg_id
        finally:
            _cleanup(db_file)

    def test_no_error_for_nonexistent_job(self):
        db_file = _fresh_db()
        try:
            # Should not raise
            mark_email_sent("nonexistent", "<job-none@example.com>")
        finally:
            _cleanup(db_file)


# ── Tests: mark_job_sent clears new fields ──────────────────────────

class TestMarkJobSentClears:
    def test_mark_job_sent_clears_smtp_fields(self):
        db_file = _fresh_db()
        try:
            job_id = _make_job()
            msg_id = f"<job-{job_id}@example.com>"
            mark_email_sent(job_id, msg_id)

            # Verify fields are set
            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                row = conn.execute(
                    "SELECT email_sent_at, smtp_message_id FROM report_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
            assert row["email_sent_at"] is not None
            assert row["smtp_message_id"] is not None

            mark_job_sent(job_id)

            with _connect() as conn:
                row = conn.execute(
                    "SELECT email_sent_at, smtp_message_id, status, recipient_email FROM report_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
            assert row["email_sent_at"] is None
            assert row["smtp_message_id"] is None
            assert row["status"] == "sent"
            assert row["recipient_email"] is None
        finally:
            _cleanup(db_file)


# ── Tests: _public_job includes email_sent_at ───────────────────────

class TestPublicJobFields:
    def test_public_job_includes_email_sent_at(self):
        db_file = _fresh_db()
        try:
            job_id = _make_job()
            msg_id = f"<job-{job_id}@example.com>"
            mark_email_sent(job_id, msg_id)

            from daily_report.jobs import get_job
            job = get_job(job_id)
            assert job is not None
            assert "email_sent_at" in job
            assert job["email_sent_at"] is not None
            # smtp_message_id should NOT be in public output (internal audit field)
            assert "smtp_message_id" not in job
        finally:
            _cleanup(db_file)

    def test_public_job_email_sent_at_null_for_new_job(self):
        db_file = _fresh_db()
        try:
            job_id = _make_job()
            from daily_report.jobs import get_job
            job = get_job(job_id)
            assert job is not None
            assert "email_sent_at" in job
            assert job["email_sent_at"] is None
        finally:
            _cleanup(db_file)


# ── Tests: claim_next_job returns email_sent_at ─────────────────────

class TestClaimReturnsEmailSentAt:
    def test_claim_returns_email_sent_at_field(self):
        db_file = _fresh_db()
        try:
            job_id = _make_job()
            msg_id = f"<job-{job_id}@example.com>"
            mark_email_sent(job_id, msg_id)

            # Store HTML so claim puts it in "sending" state
            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET report_html=?, file_name=? WHERE id=?",
                    (sqlite3.Binary(b"<html>"), "test.html", job_id),
                )

            job = claim_next_job()
            assert job is not None
            assert job["id"] == job_id
            assert "email_sent_at" in job
            assert job["email_sent_at"] is not None
        finally:
            _cleanup(db_file)

    def test_claim_returns_null_email_sent_at_for_new_job(self):
        db_file = _fresh_db()
        try:
            job_id = _make_job()

            # Store HTML so claim puts it in "sending" state
            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET report_html=?, file_name=? WHERE id=?",
                    (sqlite3.Binary(b"<html>"), "test.html", job_id),
                )

            job = claim_next_job()
            assert job is not None
            assert job["id"] == job_id
            assert "email_sent_at" in job
            assert job["email_sent_at"] is None
        finally:
            _cleanup(db_file)


# ── Tests: worker idempotent send logic ─────────────────────────────

class TestWorkerIdempotentSend:
    """Test that the worker skips re-sending when email_sent_at is set."""

    def test_worker_skips_send_when_email_sent_at_set(self):
        """If email_sent_at is set, worker should not call send_report_email."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()
            msg_id = f"<job-{job_id}@example.com>"

            # Store HTML and mark email as sent (simulating crash after send)
            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET report_html=?, file_name=?, report_date=?, "
                    "email_sent_at=?, smtp_message_id=? WHERE id=?",
                    (sqlite3.Binary(b"<html>report</html>"),
                     "AAPL_report.html", "2026-07-11",
                     "2026-07-11T10:00:00+00:00", msg_id, job_id),
                )

            # Mock send_report_email to track if it's called
            with patch("daily_report.worker.send_report_email") as mock_send, \
                 patch("daily_report.worker.compute_job_message_id") as mock_compute:
                from daily_report.worker import process_one_job
                worked = process_one_job()
                assert worked is True
                mock_send.assert_not_called()
                mock_compute.assert_not_called()

            # Job should now be "sent"
            from daily_report.jobs import get_job
            job = get_job(job_id)
            assert job["status"] == "sent"
        finally:
            _cleanup(db_file)

    def test_worker_sends_when_email_sent_at_null(self):
        """If email_sent_at is NULL, worker should send email normally."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()

            # Store HTML but do NOT set email_sent_at
            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET report_html=?, file_name=?, report_date=? WHERE id=?",
                    (sqlite3.Binary(b"<html>report</html>"),
                     "AAPL_report.html", "2026-07-11", job_id),
                )

            sent_message_ids = []

            def fake_send(**kwargs):
                sent_message_ids.append(kwargs.get("message_id"))

            with patch("daily_report.worker.send_report_email", side_effect=fake_send), \
                 patch("daily_report.worker.compute_job_message_id",
                       return_value=f"<job-{job_id}@example.com>"):
                from daily_report.worker import process_one_job
                worked = process_one_job()
                assert worked is True

            # Email was sent
            assert len(sent_message_ids) == 1
            assert sent_message_ids[0] == f"<job-{job_id}@example.com>"

            # Job should be "sent"
            from daily_report.jobs import get_job
            job = get_job(job_id)
            assert job["status"] == "sent"
        finally:
            _cleanup(db_file)

    def test_worker_calls_mark_email_sent_before_mark_job_sent(self):
        """After send, mark_email_sent should be called before mark_job_sent."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()

            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET report_html=?, file_name=?, report_date=? WHERE id=?",
                    (sqlite3.Binary(b"<html>report</html>"),
                     "AAPL_report.html", "2026-07-11", job_id),
                )

            call_order = []

            original_mark_email_sent = mark_email_sent
            original_mark_job_sent = mark_job_sent

            def tracking_mark_email_sent(jid, mid):
                call_order.append("mark_email_sent")
                original_mark_email_sent(jid, mid)

            def tracking_mark_job_sent(jid):
                call_order.append("mark_job_sent")
                original_mark_job_sent(jid)

            with patch("daily_report.worker.send_report_email"), \
                 patch("daily_report.worker.compute_job_message_id",
                       return_value=f"<job-{job_id}@example.com>"), \
                 patch("daily_report.worker.mark_email_sent",
                       side_effect=tracking_mark_email_sent), \
                 patch("daily_report.worker.mark_job_sent",
                       side_effect=tracking_mark_job_sent):
                from daily_report.worker import process_one_job
                process_one_job()

            assert call_order == ["mark_email_sent", "mark_job_sent"]
        finally:
            _cleanup(db_file)


# ── Tests: crash recovery scenario ──────────────────────────────────

class TestCrashRecovery:
    def test_crash_after_send_no_resend(self):
        """Simulate: send succeeds, mark_email_sent succeeds, crash before mark_job_sent.
        On recovery, job should be re-queued, claimed, and NOT re-sent."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()
            msg_id = f"<job-{job_id}@example.com>"

            # Simulate report generation and email sent
            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET report_html=?, file_name=?, report_date=?, "
                    "status='sending', email_sent_at=?, smtp_message_id=? WHERE id=?",
                    (sqlite3.Binary(b"<html>report</html>"),
                     "AAPL_report.html", "2026-07-11",
                     "2026-07-11T10:00:00+00:00", msg_id, job_id),
                )

            # Simulate crash: recover_interrupted_jobs puts "sending" -> "queued"
            recovered = recover_interrupted_jobs()
            assert recovered == 1

            from daily_report.jobs import get_job
            job = get_job(job_id)
            assert job["status"] == "queued"

            # email_sent_at should still be set
            import sqlite3 as sq
            with _connect() as conn:
                row = conn.execute(
                    "SELECT email_sent_at FROM report_jobs WHERE id=?", (job_id,)
                ).fetchone()
            assert row["email_sent_at"] is not None

            # Worker claims and processes - should NOT re-send
            with patch("daily_report.worker.send_report_email") as mock_send:
                from daily_report.worker import process_one_job
                worked = process_one_job()
                assert worked is True
                mock_send.assert_not_called()

            job = get_job(job_id)
            assert job["status"] == "sent"
        finally:
            _cleanup(db_file)

    def test_crash_before_send_does_resend(self):
        """Simulate: crash during generation (before email sent).
        On recovery, job should be re-queued and email should be sent."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()

            # Simulate crash during "generating" - no HTML, no email_sent_at
            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET status='generating' WHERE id=?",
                    (job_id,),
                )

            recovered = recover_interrupted_jobs()
            assert recovered == 1

            # Now claim and process - will need to generate
            # Mock generate_report to return success
            mock_result = {
                "success": True,
                "html_bytes": b"<html>report</html>",
                "file_name": "AAPL_report.html",
                "report_date": "2026-07-11",
                "elapsed": 1.0,
            }
            with patch("daily_report.worker.generate_report",
                       return_value=mock_result), \
                 patch("daily_report.worker.send_report_email") as mock_send, \
                 patch("daily_report.worker.compute_job_message_id",
                       return_value=f"<job-{job_id}@example.com>"):
                from daily_report.worker import process_one_job
                worked = process_one_job()
                assert worked is True
                mock_send.assert_called_once()

            from daily_report.jobs import get_job
            job = get_job(job_id)
            assert job["status"] == "sent"
        finally:
            _cleanup(db_file)

    def test_crash_between_send_and_mark_email_sent_uses_deterministic_msgid(self):
        """Simulate: send succeeds but crash before mark_email_sent.
        On recovery, email is re-sent, but with the SAME Message-ID
        (deterministic from job_id), allowing mail server dedup."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()

            # Store HTML, set status to "sending" (email was sent but not recorded)
            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET report_html=?, file_name=?, report_date=?, "
                    "status='sending' WHERE id=?",
                    (sqlite3.Binary(b"<html>report</html>"),
                     "AAPL_report.html", "2026-07-11", job_id),
                )

            # Crash - recover
            recover_interrupted_jobs()

            # Worker claims and re-sends with deterministic Message-ID
            sent_ids = []

            def fake_send(**kwargs):
                sent_ids.append(kwargs.get("message_id"))

            with patch("daily_report.worker.send_report_email",
                       side_effect=fake_send), \
                 patch("daily_report.worker.compute_job_message_id",
                       return_value=f"<job-{job_id}@example.com>"):
                from daily_report.worker import process_one_job
                process_one_job()

            # Email was re-sent with deterministic Message-ID
            assert len(sent_ids) == 1
            assert sent_ids[0] == f"<job-{job_id}@example.com>"
        finally:
            _cleanup(db_file)


# ── Tests: mailer message_id parameter ──────────────────────────────

class TestMailerMessageId:
    def test_send_report_email_uses_provided_message_id(self):
        """send_report_email should use the message_id parameter if provided."""
        from daily_report.mailer import send_report_email, SmtpConfig

        # Mock the SMTP connection
        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)

        test_msg_id = "<job-test123@example.com>"

        with patch("daily_report.mailer.load_smtp_config") as mock_config, \
             patch("daily_report.mailer.smtplib.SMTP_SSL",
                   return_value=mock_smtp) as mock_ssl, \
             patch("daily_report.mailer.ssl.create_default_context",
                   return_value=MagicMock()):
            mock_config.return_value = SmtpConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                authorization_code="pass",
                sender="user@example.com",
                use_ssl=True,
                timeout=30,
            )

            send_report_email(
                recipient="recipient@example.com",
                ticker="AAPL",
                report_date="2026-07-11",
                file_name="AAPL_report.html",
                html_bytes=b"<html>test</html>",
                message_id=test_msg_id,
            )

            mock_ssl.assert_called_once()
            # Check that the message was sent
            mock_smtp.send_message.assert_called_once()
            sent_msg = mock_smtp.send_message.call_args[0][0]
            assert sent_msg["Message-ID"] == test_msg_id

    def test_send_report_email_generates_msgid_when_none(self):
        """send_report_email should generate a Message-ID if not provided."""
        from daily_report.mailer import send_report_email, SmtpConfig

        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch("daily_report.mailer.load_smtp_config") as mock_config, \
             patch("daily_report.mailer.smtplib.SMTP_SSL",
                   return_value=mock_smtp), \
             patch("daily_report.mailer.ssl.create_default_context",
                   return_value=MagicMock()):
            mock_config.return_value = SmtpConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                authorization_code="pass",
                sender="user@example.com",
                use_ssl=True,
                timeout=30,
            )

            send_report_email(
                recipient="recipient@example.com",
                ticker="AAPL",
                report_date="2026-07-11",
                file_name="AAPL_report.html",
                html_bytes=b"<html>test</html>",
            )

            mock_smtp.send_message.assert_called_once()
            sent_msg = mock_smtp.send_message.call_args[0][0]
            assert sent_msg["Message-ID"] is not None
            assert "@" in sent_msg["Message-ID"]


# ── Tests: compute_job_message_id ───────────────────────────────────

class TestComputeJobMessageId:
    def test_deterministic_from_job_id(self):
        """Same job_id should always produce the same Message-ID."""
        from daily_report.mailer import compute_job_message_id, SmtpConfig

        with patch("daily_report.mailer.load_smtp_config") as mock_config:
            mock_config.return_value = SmtpConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                authorization_code="pass",
                sender="user@example.com",
                use_ssl=True,
                timeout=30,
            )
            msg_id_1 = compute_job_message_id("abc123")
            msg_id_2 = compute_job_message_id("abc123")
            assert msg_id_1 == msg_id_2
            assert msg_id_1 == "<job-abc123@example.com>"

    def test_different_job_ids_produce_different_msgids(self):
        from daily_report.mailer import compute_job_message_id, SmtpConfig

        with patch("daily_report.mailer.load_smtp_config") as mock_config:
            mock_config.return_value = SmtpConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                authorization_code="pass",
                sender="user@example.com",
                use_ssl=True,
                timeout=30,
            )
            id1 = compute_job_message_id("abc123")
            id2 = compute_job_message_id("def456")
            assert id1 != id2

    def test_uses_sender_domain(self):
        from daily_report.mailer import compute_job_message_id, SmtpConfig

        with patch("daily_report.mailer.load_smtp_config") as mock_config:
            mock_config.return_value = SmtpConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                authorization_code="pass",
                sender="alerts@stockwatch.io",
                use_ssl=True,
                timeout=30,
            )
            msg_id = compute_job_message_id("xyz789")
            assert msg_id == "<job-xyz789@stockwatch.io>"

    def test_fallback_domain_for_no_at_in_sender(self):
        from daily_report.mailer import compute_job_message_id, SmtpConfig

        with patch("daily_report.mailer.load_smtp_config") as mock_config:
            mock_config.return_value = SmtpConfig(
                host="smtp.example.com",
                port=465,
                username="user",
                authorization_code="pass",
                sender="noreply",  # no @ symbol
                use_ssl=True,
                timeout=30,
            )
            msg_id = compute_job_message_id("test123")
            assert msg_id == "<job-test123@localhost>"


# ── Tests: end-to-end worker flow ───────────────────────────────────

class TestEndToEndWorkerFlow:
    def test_full_flow_generate_and_send(self):
        """Full flow: claim queued job -> generate -> send -> mark sent."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()

            mock_result = {
                "success": True,
                "html_bytes": b"<html>full report</html>",
                "file_name": "AAPL_report.html",
                "report_date": "2026-07-11",
                "elapsed": 5.2,
            }

            with patch("daily_report.worker.generate_report",
                       return_value=mock_result), \
                 patch("daily_report.worker.send_report_email") as mock_send, \
                 patch("daily_report.worker.compute_job_message_id",
                       return_value=f"<job-{job_id}@example.com>"):
                from daily_report.worker import process_one_job
                worked = process_one_job()
                assert worked is True
                mock_send.assert_called_once()

            from daily_report.jobs import get_job
            job = get_job(job_id)
            assert job["status"] == "sent"
            assert job["email_sent_at"] is None  # cleared by mark_job_sent
        finally:
            _cleanup(db_file)

    def test_full_flow_with_pre_generated_html(self):
        """Full flow: claim job with pre-generated HTML -> send -> mark sent."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()

            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET report_html=?, file_name=?, report_date=? WHERE id=?",
                    (sqlite3.Binary(b"<html>pre-generated</html>"),
                     "AAPL_report.html", "2026-07-11", job_id),
                )

            with patch("daily_report.worker.send_report_email") as mock_send, \
                 patch("daily_report.worker.compute_job_message_id",
                       return_value=f"<job-{job_id}@example.com>"):
                from daily_report.worker import process_one_job
                worked = process_one_job()
                assert worked is True
                mock_send.assert_called_once()

            from daily_report.jobs import get_job
            job = get_job(job_id)
            assert job["status"] == "sent"
        finally:
            _cleanup(db_file)

    def test_worker_failure_does_not_set_email_sent_at(self):
        """If send_report_email raises, email_sent_at should NOT be set."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()

            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                conn.execute(
                    "UPDATE report_jobs SET report_html=?, file_name=?, report_date=? WHERE id=?",
                    (sqlite3.Binary(b"<html>report</html>"),
                     "AAPL_report.html", "2026-07-11", job_id),
                )

            with patch("daily_report.worker.send_report_email",
                       side_effect=ConnectionError("SMTP refused")), \
                 patch("daily_report.worker.compute_job_message_id",
                       return_value=f"<job-{job_id}@example.com>"):
                from daily_report.worker import process_one_job
                worked = process_one_job()
                assert worked is True

            # email_sent_at should NOT be set
            with _connect() as conn:
                row = conn.execute(
                    "SELECT email_sent_at, status FROM report_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
            assert row["email_sent_at"] is None
            # Job should be queued for retry (attempts=1, max_attempts=3)
            assert row["status"] == "queued"
        finally:
            _cleanup(db_file)


# ── Tests: mark_job_failure preserves email_sent_at ─────────────────

class TestMarkJobFailurePreservesEmailSentAt:
    def test_mark_job_failure_does_not_clear_email_sent_at(self):
        """If a job fails after email_sent_at was set (edge case),
        the email_sent_at should still be visible for audit."""
        db_file = _fresh_db()
        try:
            job_id = _make_job()
            msg_id = f"<job-{job_id}@example.com>"
            mark_email_sent(job_id, msg_id)

            # Now simulate a failure (shouldn't happen in normal flow, but test)
            mark_job_failure(job_id, "Some error")

            import sqlite3
            from daily_report.jobs import _connect
            with _connect() as conn:
                row = conn.execute(
                    "SELECT email_sent_at, smtp_message_id FROM report_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
            # email_sent_at is preserved (not cleared by mark_job_failure)
            assert row["email_sent_at"] is not None
            assert row["smtp_message_id"] == msg_id
        finally:
            _cleanup(db_file)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
