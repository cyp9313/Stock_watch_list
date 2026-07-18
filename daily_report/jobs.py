from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
from email.utils import parseaddr
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid
from zoneinfo import ZoneInfo


APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOB_DB = APP_ROOT / "daily_report_jobs.db"
ACTIVE_STATUSES = ("queued", "generating", "sending")
RUNNING_STATUSES = ("generating", "sending")
EXPIRED_STATUS = "expired"
SCHEDULE_TIMEZONE = "Europe/Berlin"
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_INIT_LOCK = threading.Lock()
_INITIALIZED_DATABASES: set[str] = set()


class JobQueueError(RuntimeError):
    pass


class ActiveJobError(JobQueueError):
    pass


class DailyLimitError(JobQueueError):
    pass


class ScheduleLimitError(JobQueueError):
    pass


class QueueFullError(JobQueueError):
    """Raised when the global or per-user pending limit would be exceeded."""
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _max_global_pending() -> int:
    return max(1, int(os.environ.get("REPORT_MAX_GLOBAL_PENDING", "50")))


def _max_pending_per_user() -> int:
    return max(1, int(os.environ.get("REPORT_MAX_PENDING_PER_USER", "5")))


def _max_global_running() -> int:
    return max(1, int(os.environ.get("REPORT_MAX_GLOBAL_RUNNING", "1")))


def _max_queue_hours() -> float:
    return max(0.5, float(os.environ.get("REPORT_MAX_QUEUE_HOURS", "6")))


def _db_path() -> Path:
    configured = os.environ.get("REPORT_JOB_DB", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_JOB_DB


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def _connection():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_job_db() -> None:
    database_key = str(_db_path())
    if database_key in _INITIALIZED_DATABASES:
        return
    with _INIT_LOCK:
        if database_key in _INITIALIZED_DATABASES:
            return
        with _connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS report_jobs (
                    id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    recipient_email TEXT,
                    recipient_masked TEXT NOT NULL,
                    months INTEGER NOT NULL,
                    search_provider TEXT NOT NULL,
                    no_article_fetch INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    next_attempt_at TEXT,
                    generation_seconds REAL,
                    report_date TEXT,
                    file_name TEXT,
                    report_html BLOB,
                    last_error TEXT
                )
                """
            )
            job_columns = {row[1] for row in conn.execute("PRAGMA table_info(report_jobs)")}
            if "schedule_id" not in job_columns:
                conn.execute("ALTER TABLE report_jobs ADD COLUMN schedule_id TEXT")
            if "scheduled_for" not in job_columns:
                conn.execute("ALTER TABLE report_jobs ADD COLUMN scheduled_for TEXT")
            if "smtp_message_id" not in job_columns:
                conn.execute("ALTER TABLE report_jobs ADD COLUMN smtp_message_id TEXT")
            if "email_sent_at" not in job_columns:
                conn.execute("ALTER TABLE report_jobs ADD COLUMN email_sent_at TEXT")
            if "report_kind" not in job_columns:
                conn.execute("ALTER TABLE report_jobs ADD COLUMN report_kind TEXT NOT NULL DEFAULT 'ticker'")
            if "subject_key" not in job_columns:
                conn.execute("ALTER TABLE report_jobs ADD COLUMN subject_key TEXT")
            if "subject_name" not in job_columns:
                conn.execute("ALTER TABLE report_jobs ADD COLUMN subject_name TEXT")
            if "payload_json" not in job_columns:
                conn.execute("ALTER TABLE report_jobs ADD COLUMN payload_json TEXT")
            conn.execute(
                "UPDATE report_jobs SET report_kind='ticker' "
                "WHERE report_kind IS NULL OR report_kind=''"
            )
            conn.execute(
                "UPDATE report_jobs SET subject_key=ticker "
                "WHERE subject_key IS NULL OR subject_key=''"
            )
            conn.execute(
                "UPDATE report_jobs SET subject_name=ticker "
                "WHERE subject_name IS NULL OR subject_name=''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_report_jobs_queue "
                "ON report_jobs(status, next_attempt_at, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_report_jobs_owner "
                "ON report_jobs(owner_key, created_at DESC)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_report_jobs_schedule_occurrence "
                "ON report_jobs(schedule_id, scheduled_for)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS report_schedules (
                    id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    recipient_email TEXT NOT NULL,
                    recipient_masked TEXT NOT NULL,
                    weekday INTEGER NOT NULL,
                    weekdays_json TEXT,
                    local_time TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Europe/Berlin',
                    months INTEGER NOT NULL,
                    search_provider TEXT NOT NULL,
                    no_article_fetch INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_run_at TEXT,
                    last_enqueued_at TEXT,
                    last_scheduled_for TEXT
                )
                """
            )
            schedule_columns = {row[1] for row in conn.execute("PRAGMA table_info(report_schedules)")}
            if "weekdays_json" not in schedule_columns:
                conn.execute("ALTER TABLE report_schedules ADD COLUMN weekdays_json TEXT")
            if "report_kind" not in schedule_columns:
                conn.execute("ALTER TABLE report_schedules ADD COLUMN report_kind TEXT NOT NULL DEFAULT 'ticker'")
            if "subject_key" not in schedule_columns:
                conn.execute("ALTER TABLE report_schedules ADD COLUMN subject_key TEXT")
            if "subject_name" not in schedule_columns:
                conn.execute("ALTER TABLE report_schedules ADD COLUMN subject_name TEXT")
            if "payload_json" not in schedule_columns:
                conn.execute("ALTER TABLE report_schedules ADD COLUMN payload_json TEXT")
            # Existing schedules were one-row-per-weekday.  Preserve their
            # behaviour by treating the legacy weekday as a one-item set.
            conn.execute(
                "UPDATE report_schedules SET weekdays_json='[' || weekday || ']' "
                "WHERE weekdays_json IS NULL OR weekdays_json=''"
            )
            conn.execute(
                "UPDATE report_schedules SET report_kind='ticker' "
                "WHERE report_kind IS NULL OR report_kind=''"
            )
            conn.execute(
                "UPDATE report_schedules SET subject_key=ticker "
                "WHERE subject_key IS NULL OR subject_key=''"
            )
            conn.execute(
                "UPDATE report_schedules SET subject_name=ticker "
                "WHERE subject_name IS NULL OR subject_name=''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_report_schedules_due "
                "ON report_schedules(is_active, next_run_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_report_schedules_owner "
                "ON report_schedules(owner_key, created_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS download_generations (
                    id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    elapsed REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_download_generations_owner "
                "ON download_generations(owner_key, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_download_generations_status "
                "ON download_generations(status, created_at)"
            )
        _INITIALIZED_DATABASES.add(database_key)


def validate_email(email: str) -> str:
    email = str(email or "").strip()
    if not email or len(email) > 254 or "\n" in email or "\r" in email:
        raise ValueError("Please enter a valid email address.")
    _, parsed = parseaddr(email)
    if parsed != email or not _EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Please enter a valid email address.")
    return email


def mask_email(email: str) -> str:
    local, domain = email.rsplit("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}{'*' * max(3, len(local) - len(visible))}@{domain}"


def _normalize_local_time(value: str | dt.time) -> str:
    if isinstance(value, dt.time):
        value = value.strftime("%H:%M")
    value = str(value or "").strip()
    if not _TIME_PATTERN.fullmatch(value):
        raise ValueError("Weekly send time must use HH:MM in Europe/Berlin time.")
    return value


def next_weekly_run_at(
    weekday: int,
    local_time: str | dt.time,
    *,
    after_utc: dt.datetime | None = None,
    timezone_name: str = SCHEDULE_TIMEZONE,
) -> dt.datetime:
    weekday = int(weekday)
    if weekday not in range(7):
        raise ValueError("Weekday must be between Monday and Sunday.")
    local_time = _normalize_local_time(local_time)
    hour, minute = (int(part) for part in local_time.split(":"))
    timezone = ZoneInfo(timezone_name)
    after_utc = after_utc or _now()
    if after_utc.tzinfo is None:
        after_utc = after_utc.replace(tzinfo=dt.timezone.utc)
    else:
        after_utc = after_utc.astimezone(dt.timezone.utc)
    local_after = after_utc.astimezone(timezone)

    days_ahead = (weekday - local_after.weekday()) % 7
    candidate_date = local_after.date() + dt.timedelta(days=days_ahead)
    for _ in range(2):
        local_candidate = dt.datetime.combine(
            candidate_date,
            dt.time(hour=hour, minute=minute),
            tzinfo=timezone,
        )
        candidate_utc = local_candidate.astimezone(dt.timezone.utc)
        # Normalizing through UTC moves nonexistent spring-forward times to
        # the first valid local time while fold=0 selects the first fall-back occurrence.
        candidate_utc = candidate_utc.astimezone(timezone).astimezone(dt.timezone.utc)
        if candidate_utc > after_utc:
            return candidate_utc
        candidate_date += dt.timedelta(days=7)
    raise RuntimeError("Unable to calculate the next weekly report time.")


def _normalize_weekdays(weekdays: object) -> tuple[int, ...]:
    """Validate and canonicalize a non-empty Monday-to-Sunday selection."""
    if isinstance(weekdays, (str, bytes)) or not isinstance(weekdays, (list, tuple, set)):
        raise ValueError("Please select at least one valid weekday.")
    try:
        selected = tuple(sorted({int(day) for day in weekdays}))
    except (TypeError, ValueError) as exc:
        raise ValueError("Please select valid weekdays.") from exc
    if not selected or any(day not in range(7) for day in selected):
        raise ValueError("Please select at least one valid weekday.")
    return selected


def _schedule_weekdays(row: sqlite3.Row | dict) -> tuple[int, ...]:
    """Return a schedule's selected weekdays, including legacy rows."""
    raw = row["weekdays_json"] if "weekdays_json" in row.keys() else None
    if raw:
        try:
            return _normalize_weekdays(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return _normalize_weekdays([row["weekday"]])


def next_scheduled_run_at(
    weekdays: object,
    local_time: str | dt.time,
    *,
    after_utc: dt.datetime | None = None,
    timezone_name: str = SCHEDULE_TIMEZONE,
) -> dt.datetime:
    """Return the earliest future occurrence across a schedule's weekdays."""
    selected = _normalize_weekdays(weekdays)
    return min(
        next_weekly_run_at(day, local_time, after_utc=after_utc, timezone_name=timezone_name)
        for day in selected
    )


def enqueue_report_job(
    *,
    owner_key: str,
    report_kind: str,
    subject_key: str,
    subject_name: str,
    recipient_email: str,
    months: int = 3,
    search_provider: str = "auto",
    no_article_fetch: bool = False,
    payload: dict | None = None,
) -> dict:
    owner_key = str(owner_key or "").strip()
    if not owner_key:
        raise ValueError("A signed-in account is required for email delivery.")
    report_kind = str(report_kind or "ticker").strip().lower()
    if report_kind not in {"ticker", "portfolio"}:
        raise ValueError("Unsupported report kind.")
    subject_key = str(subject_key or "").strip()
    subject_name = str(subject_name or subject_key).strip() or subject_key
    if not subject_key:
        raise ValueError("Report subject is required.")
    ticker = subject_key.upper() if report_kind == "ticker" else subject_name
    recipient_email = validate_email(recipient_email)
    limit_env = "PORTFOLIO_EMAIL_DAILY_LIMIT_PER_USER" if report_kind == "portfolio" else "REPORT_DAILY_LIMIT_PER_USER"
    daily_limit = max(1, int(os.environ.get(limit_env, os.environ.get("REPORT_DAILY_LIMIT_PER_USER", "3"))))
    max_attempts = max(1, int(os.environ.get("REPORT_EMAIL_MAX_ATTEMPTS", "3")))
    now = _now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))

    init_job_db()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Per-user pending limit (applies to manual AND schedule jobs)
        user_pending = conn.execute(
            "SELECT COUNT(*) FROM report_jobs WHERE owner_key=? AND status IN (?, ?, ?)",
            (owner_key, *ACTIVE_STATUSES),
        ).fetchone()[0]
        if user_pending >= _max_pending_per_user():
            raise QueueFullError(
                f"This account already has {user_pending} active report job(s). "
                f"Maximum {_max_pending_per_user()} per account."
            )

        # Global pending limit
        global_pending = conn.execute(
            "SELECT COUNT(*) FROM report_jobs WHERE status IN (?, ?, ?)",
            ACTIVE_STATUSES,
        ).fetchone()[0]
        if global_pending >= _max_global_pending():
            raise QueueFullError(
                f"The report queue is full ({global_pending}/{_max_global_pending()} pending). "
                f"Please try again later."
            )

        today_count = conn.execute(
            "SELECT COUNT(*) FROM report_jobs WHERE owner_key=? AND created_at>=? AND schedule_id IS NULL",
            (owner_key, _iso(day_start)),
        ).fetchone()[0]
        if today_count >= daily_limit:
            raise DailyLimitError(f"Daily email report limit reached ({daily_limit} per account).")

        job_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO report_jobs (
                id, owner_key, ticker, recipient_email, recipient_masked,
                months, search_provider, no_article_fetch, status,
                max_attempts, created_at, next_attempt_at,
                report_kind, subject_key, subject_name, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                owner_key,
                ticker,
                recipient_email,
                mask_email(recipient_email),
                max(1, int(months)),
                search_provider or "auto",
                int(bool(no_article_fetch)),
                max_attempts,
                _iso(now),
                _iso(now),
                report_kind,
                subject_key,
                subject_name,
                payload_json,
            ),
        )
        conn.commit()
        return get_job(job_id, owner_key=owner_key)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def enqueue_email_job(
    *,
    owner_key: str,
    ticker: str,
    recipient_email: str,
    months: int = 3,
    search_provider: str = "auto",
    no_article_fetch: bool = False,
) -> dict:
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        raise ValueError("Please enter a ticker.")
    return enqueue_report_job(
        owner_key=owner_key,
        report_kind="ticker",
        subject_key=ticker,
        subject_name=ticker,
        recipient_email=recipient_email,
        months=months,
        search_provider=search_provider,
        no_article_fetch=no_article_fetch,
        payload={"ticker": ticker},
    )


def enqueue_portfolio_email_job(
    *,
    owner_key: str,
    portfolio_page_id: str,
    portfolio_name: str,
    recipient_email: str,
    search_provider: str = "auto",
    settings: dict | None = None,
) -> dict:
    portfolio_page_id = str(portfolio_page_id or "").strip()
    portfolio_name = str(portfolio_name or "Portfolio").strip() or "Portfolio"
    if not portfolio_page_id:
        raise ValueError("Portfolio page ID is required.")
    return enqueue_report_job(
        owner_key=owner_key,
        report_kind="portfolio",
        subject_key=portfolio_page_id,
        subject_name=portfolio_name,
        recipient_email=recipient_email,
        months=3,
        search_provider=search_provider,
        no_article_fetch=False,
        payload={
            "portfolio_page_id": portfolio_page_id,
            "portfolio_name": portfolio_name,
            "search_provider": search_provider or "auto",
            "settings": settings or {},
        },
    )


def _public_job(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        key: row[key]
        for key in (
            "id",
            "owner_key",
            "ticker",
            "report_kind",
            "subject_key",
            "subject_name",
            "payload_json",
            "recipient_masked",
            "months",
            "search_provider",
            "status",
            "attempts",
            "max_attempts",
            "created_at",
            "started_at",
            "finished_at",
            "next_attempt_at",
            "generation_seconds",
            "report_date",
            "file_name",
            "last_error",
            "schedule_id",
            "scheduled_for",
            "email_sent_at",
        )
    }


def get_job(job_id: str, *, owner_key: str | None = None) -> dict | None:
    init_job_db()
    with _connection() as conn:
        if owner_key is None:
            row = conn.execute("SELECT * FROM report_jobs WHERE id=?", (job_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM report_jobs WHERE id=? AND owner_key=?",
                (job_id, owner_key),
            ).fetchone()
    return _public_job(row)


def list_owner_jobs(owner_key: str, limit: int = 10) -> list[dict]:
    init_job_db()
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM report_jobs WHERE owner_key=? ORDER BY created_at DESC LIMIT ?",
            (owner_key, max(1, min(int(limit), 50))),
        ).fetchall()
    return [_public_job(row) for row in rows]


def recover_interrupted_jobs() -> int:
    init_job_db()
    with _connection() as conn:
        cursor = conn.execute(
            """
            UPDATE report_jobs
            SET status='queued', next_attempt_at=?,
                last_error=COALESCE(last_error, 'Worker restarted; job resumed.')
            WHERE status IN ('generating', 'sending')
            """,
            (_iso(),),
        )
    return cursor.rowcount


def claim_next_job() -> dict | None:
    init_job_db()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Global running limit – prevents over-subscription when multiple
        # workers are active.  Default 1 matches the single-worker design.
        max_running = _max_global_running()
        running = conn.execute(
            "SELECT COUNT(*) FROM report_jobs WHERE status IN (?, ?)",
            RUNNING_STATUSES,
        ).fetchone()[0]
        if running >= max_running:
            conn.commit()
            return None

        row = conn.execute(
            """
            SELECT * FROM report_jobs
            WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY created_at ASC LIMIT 1
            """,
            (_iso(),),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        next_status = "sending" if row["report_html"] is not None else "generating"
        started_at = row["started_at"] or _iso()
        conn.execute(
            """
            UPDATE report_jobs
            SET status=?, attempts=attempts+1, started_at=?, next_attempt_at=NULL
            WHERE id=?
            """,
            (next_status, started_at, row["id"]),
        )
        conn.commit()
        claimed = dict(row)
        claimed["status"] = next_status
        claimed["attempts"] = int(row["attempts"]) + 1
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def store_generated_report(
    job_id: str,
    html_bytes: bytes,
    file_name: str,
    report_date: str,
    elapsed: float,
) -> None:
    with _connection() as conn:
        conn.execute(
            """
            UPDATE report_jobs
            SET status='sending', report_html=?, file_name=?, report_date=?, generation_seconds=?, last_error=NULL
            WHERE id=?
            """,
            (sqlite3.Binary(html_bytes), file_name, report_date, float(elapsed), job_id),
        )


def mark_email_sent(job_id: str, message_id: str) -> None:
    """Record that the SMTP send returned success for this job.

    Idempotent: if ``email_sent_at`` is already set the call is a no-op,
    so a retry after a crash between *this* write and ``mark_job_sent``
    will not overwrite the original timestamp.
    """
    with _connection() as conn:
        conn.execute(
            """
            UPDATE report_jobs
            SET email_sent_at=?, smtp_message_id=?
            WHERE id=? AND email_sent_at IS NULL
            """,
            (_iso(), message_id, job_id),
        )


def mark_job_sent(job_id: str) -> None:
    with _connection() as conn:
        conn.execute(
            """
            UPDATE report_jobs
            SET status='sent', finished_at=?, next_attempt_at=NULL,
                recipient_email=NULL, report_html=NULL, last_error=NULL,
                smtp_message_id=NULL, email_sent_at=NULL
            WHERE id=?
            """,
            (_iso(), job_id),
        )


def mark_job_failure(job_id: str, error: str, *, retry: bool = True) -> str:
    error = str(error or "Unknown error")[-4000:]
    with _connection() as conn:
        row = conn.execute(
            "SELECT attempts, max_attempts FROM report_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            return "missing"
        if retry and int(row["attempts"]) < int(row["max_attempts"]):
            delay = min(900, 60 * (2 ** max(0, int(row["attempts"]) - 1)))
            retry_at = _now() + dt.timedelta(seconds=delay)
            conn.execute(
                """
                UPDATE report_jobs
                SET status='queued', next_attempt_at=?, last_error=?
                WHERE id=?
                """,
                (_iso(retry_at), error, job_id),
            )
            return "queued"
        conn.execute(
            """
            UPDATE report_jobs
            SET status='failed', finished_at=?, next_attempt_at=NULL,
                recipient_email=NULL, report_html=NULL, last_error=?
            WHERE id=?
            """,
            (_iso(), error, job_id),
        )
        return "failed"


def expire_stale_queued_jobs(now_utc: dt.datetime | None = None) -> int:
    """Mark queued jobs older than ``REPORT_MAX_QUEUE_HOURS`` as expired.

    Expired jobs are no longer processed by the worker — their report content
    would be stale and potentially misleading (e.g. a Monday morning report
    sent on Wednesday).
    """
    now_utc = now_utc or _now()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    else:
        now_utc = now_utc.astimezone(dt.timezone.utc)
    max_hours = _max_queue_hours()
    cutoff = now_utc - dt.timedelta(hours=max_hours)
    with _connection() as conn:
        cursor = conn.execute(
            """
            UPDATE report_jobs
            SET status='expired', finished_at=?, next_attempt_at=NULL,
                last_error=COALESCE(last_error,
                    'Job expired: queued longer than the maximum queue time.')
            WHERE status='queued' AND created_at<?
            """,
            (_iso(now_utc), _iso(cutoff)),
        )
    return cursor.rowcount


def prune_old_jobs(retention_days: int | None = None) -> int:
    retention_days = retention_days or int(os.environ.get("REPORT_JOB_RETENTION_DAYS", "7"))
    cutoff = _now() - dt.timedelta(days=max(1, retention_days))
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM report_jobs WHERE status IN ('sent', 'failed', 'expired') AND finished_at<?",
            (_iso(cutoff),),
        )
    return cursor.rowcount


def create_report_schedule(
    *,
    owner_key: str,
    report_kind: str,
    subject_key: str,
    subject_name: str,
    recipient_email: str,
    weekday: int | None = None,
    local_time: str | dt.time,
    weekdays: object | None = None,
    months: int = 3,
    search_provider: str = "auto",
    no_article_fetch: bool = False,
    payload: dict | None = None,
) -> dict:
    owner_key = str(owner_key or "").strip()
    if not owner_key:
        raise ValueError("A signed-in account is required for weekly reports.")
    report_kind = str(report_kind or "ticker").strip().lower()
    if report_kind not in {"ticker", "portfolio"}:
        raise ValueError("Unsupported report kind.")
    subject_key = str(subject_key or "").strip()
    subject_name = str(subject_name or subject_key).strip() or subject_key
    if not subject_key:
        raise ValueError("Report subject is required.")
    ticker = subject_key.upper() if report_kind == "ticker" else subject_name
    recipient_email = validate_email(recipient_email)
    selected_weekdays = _normalize_weekdays(
        weekdays if weekdays is not None else [weekday]
    )
    # Keep the legacy column populated for older databases and callers.  New
    # behaviour is driven by weekdays_json, which represents one ticker plan.
    weekday = selected_weekdays[0]
    weekdays_json = json.dumps(selected_weekdays, separators=(",", ":"))
    local_time = _normalize_local_time(local_time)
    schedule_limit_env = "PORTFOLIO_MAX_SCHEDULES_PER_USER" if report_kind == "portfolio" else "REPORT_MAX_SCHEDULES_PER_USER"
    max_schedules = min(7, max(1, int(os.environ.get(schedule_limit_env, os.environ.get("REPORT_MAX_SCHEDULES_PER_USER", "7")))))
    now = _now()
    next_run = next_scheduled_run_at(selected_weekdays, local_time, after_utc=now)
    payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))

    init_job_db()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        schedule_count = conn.execute(
            "SELECT COUNT(*) FROM report_schedules WHERE owner_key=?",
            (owner_key,),
        ).fetchone()[0]
        if schedule_count >= max_schedules:
            raise ScheduleLimitError(f"Weekly schedule limit reached ({max_schedules} per account).")
        duplicates = conn.execute(
            """
            SELECT id, weekday, weekdays_json FROM report_schedules
            WHERE owner_key=? AND report_kind=? AND subject_key=? AND recipient_email=? AND local_time=?
            """,
            (owner_key, report_kind, subject_key, recipient_email, local_time),
        ).fetchall()
        if any(_schedule_weekdays(row) == selected_weekdays for row in duplicates):
            raise ValueError("An identical weekly schedule already exists for this account.")
        schedule_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO report_schedules (
                id, owner_key, ticker, recipient_email, recipient_masked,
                weekday, weekdays_json, local_time, timezone, months, search_provider,
                no_article_fetch, is_active, created_at, updated_at, next_run_at,
                report_kind, subject_key, subject_name, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                schedule_id,
                owner_key,
                ticker,
                recipient_email,
                mask_email(recipient_email),
                weekday,
                weekdays_json,
                local_time,
                SCHEDULE_TIMEZONE,
                max(1, int(months)),
                search_provider or "auto",
                int(bool(no_article_fetch)),
                _iso(now),
                _iso(now),
                _iso(next_run),
                report_kind,
                subject_key,
                subject_name,
                payload_json,
            ),
        )
        conn.commit()
        return get_schedule(schedule_id, owner_key=owner_key)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_weekly_schedule(
    *,
    owner_key: str,
    ticker: str,
    recipient_email: str,
    weekday: int | None = None,
    local_time: str | dt.time,
    weekdays: object | None = None,
    months: int = 3,
    search_provider: str = "auto",
    no_article_fetch: bool = False,
) -> dict:
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        raise ValueError("Please enter a ticker.")
    return create_report_schedule(
        owner_key=owner_key,
        report_kind="ticker",
        subject_key=ticker,
        subject_name=ticker,
        recipient_email=recipient_email,
        weekday=weekday,
        local_time=local_time,
        weekdays=weekdays,
        months=months,
        search_provider=search_provider,
        no_article_fetch=no_article_fetch,
        payload={"ticker": ticker},
    )


def create_weekly_portfolio_schedule(
    *,
    owner_key: str,
    portfolio_page_id: str,
    portfolio_name: str,
    recipient_email: str,
    weekday: int | None = None,
    local_time: str | dt.time,
    weekdays: object | None = None,
    search_provider: str = "auto",
    settings: dict | None = None,
) -> dict:
    return create_report_schedule(
        owner_key=owner_key,
        report_kind="portfolio",
        subject_key=str(portfolio_page_id or "").strip(),
        subject_name=str(portfolio_name or "Portfolio").strip() or "Portfolio",
        recipient_email=recipient_email,
        weekday=weekday,
        local_time=local_time,
        weekdays=weekdays,
        months=3,
        search_provider=search_provider,
        no_article_fetch=False,
        payload={
            "portfolio_page_id": str(portfolio_page_id or "").strip(),
            "portfolio_name": str(portfolio_name or "Portfolio").strip() or "Portfolio",
            "search_provider": search_provider or "auto",
            "settings": settings or {},
        },
    )


def _public_schedule(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    schedule = {
        key: row[key]
        for key in (
            "id",
            "owner_key",
            "ticker",
            "report_kind",
            "subject_key",
            "subject_name",
            "payload_json",
            "recipient_masked",
            "weekday",
            "local_time",
            "timezone",
            "months",
            "search_provider",
            "no_article_fetch",
            "is_active",
            "created_at",
            "updated_at",
            "next_run_at",
            "last_enqueued_at",
            "last_scheduled_for",
        )
    }
    schedule["weekdays"] = list(_schedule_weekdays(row))
    return schedule


def get_schedule(schedule_id: str, *, owner_key: str | None = None) -> dict | None:
    init_job_db()
    with _connection() as conn:
        if owner_key is None:
            row = conn.execute("SELECT * FROM report_schedules WHERE id=?", (schedule_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM report_schedules WHERE id=? AND owner_key=?",
                (schedule_id, owner_key),
            ).fetchone()
    return _public_schedule(row)


def list_owner_schedules(owner_key: str) -> list[dict]:
    init_job_db()
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM report_schedules WHERE owner_key=? ORDER BY created_at DESC",
            (owner_key,),
        ).fetchall()
    return [_public_schedule(row) for row in rows]


def set_schedule_active(schedule_id: str, *, owner_key: str, active: bool) -> bool:
    init_job_db()
    with _connection() as conn:
        row = conn.execute(
            "SELECT weekday, weekdays_json, local_time, timezone FROM report_schedules WHERE id=? AND owner_key=?",
            (schedule_id, owner_key),
        ).fetchone()
        if row is None:
            return False
        next_run = (
            _iso(next_scheduled_run_at(_schedule_weekdays(row), row["local_time"], timezone_name=row["timezone"]))
            if active
            else None
        )
        conn.execute(
            "UPDATE report_schedules SET is_active=?, next_run_at=?, updated_at=? WHERE id=?",
            (int(bool(active)), next_run, _iso(), schedule_id),
        )
    return True


def delete_schedule(schedule_id: str, *, owner_key: str) -> bool:
    init_job_db()
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM report_schedules WHERE id=? AND owner_key=?",
            (schedule_id, owner_key),
        )
    return cursor.rowcount > 0


def materialize_due_schedules(now_utc: dt.datetime | None = None) -> int:
    """Turn due weekly schedules into durable email jobs exactly once."""
    now_utc = now_utc or _now()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    else:
        now_utc = now_utc.astimezone(dt.timezone.utc)
    now_iso = _iso(now_utc)
    max_attempts = max(1, int(os.environ.get("REPORT_EMAIL_MAX_ATTEMPTS", "3")))
    created = 0

    init_job_db()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        max_global = _max_global_pending()
        max_per_user = _max_pending_per_user()

        # Snapshot global pending once; increment locally as we create jobs.
        global_pending = conn.execute(
            "SELECT COUNT(*) FROM report_jobs WHERE status IN (?, ?, ?)",
            ACTIVE_STATUSES,
        ).fetchone()[0]

        schedules = conn.execute(
            """
            SELECT * FROM report_schedules
            WHERE is_active=1 AND next_run_at IS NOT NULL AND next_run_at<=?
            ORDER BY next_run_at ASC
            """,
            (now_iso,),
        ).fetchall()
        for schedule in schedules:
            # Global pending limit — stop all materialization
            if global_pending >= max_global:
                break

            # Per-user pending limit — skip this schedule, don't advance next_run_at
            user_pending = conn.execute(
                "SELECT COUNT(*) FROM report_jobs WHERE owner_key=? AND status IN (?, ?, ?)",
                (schedule["owner_key"], *ACTIVE_STATUSES),
            ).fetchone()[0]
            if user_pending >= max_per_user:
                continue

            scheduled_for = schedule["next_run_at"]
            job_id = uuid.uuid4().hex
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO report_jobs (
                    id, owner_key, ticker, recipient_email, recipient_masked,
                    months, search_provider, no_article_fetch, status,
                    max_attempts, created_at, next_attempt_at, schedule_id, scheduled_for,
                    report_kind, subject_key, subject_name, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    schedule["owner_key"],
                    schedule["ticker"],
                    schedule["recipient_email"],
                    schedule["recipient_masked"],
                    schedule["months"],
                    schedule["search_provider"],
                    schedule["no_article_fetch"],
                    max_attempts,
                    now_iso,
                    now_iso,
                    schedule["id"],
                    scheduled_for,
                    schedule["report_kind"] or "ticker",
                    schedule["subject_key"] or schedule["ticker"],
                    schedule["subject_name"] or schedule["ticker"],
                    schedule["payload_json"],
                ),
            )
            if cursor.rowcount > 0:
                created += 1
                global_pending += 1

            # Always advance next_run_at — whether we just created the job
            # or it already existed (dedup).  Schedules that were skipped
            # due to per-user limit do NOT get advanced, so the same
            # occurrence is retried in the next materialization cycle.
            next_run = next_scheduled_run_at(
                _schedule_weekdays(schedule),
                schedule["local_time"],
                after_utc=now_utc,
                timezone_name=schedule["timezone"],
            )
            conn.execute(
                """
                UPDATE report_schedules
                SET next_run_at=?, last_enqueued_at=?, last_scheduled_for=?, updated_at=?
                WHERE id=?
                """,
                (_iso(next_run), now_iso, scheduled_for, now_iso, schedule["id"]),
            )
        conn.commit()
        return created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Synchronous download generation rate-limiting ────────────────

DOWNLOAD_ACTIVE_STATUSES = ("generating",)
_DOWNLOAD_STALE_SECONDS = 1860  # DEFAULT_TIMEOUT_SECONDS (1800) + 60s grace


def cleanup_stale_download_generations(now: dt.datetime | None = None) -> int:
    """Mark download generations stuck in 'generating' as 'failed'."""
    now = now or _now()
    cutoff = now - dt.timedelta(seconds=_DOWNLOAD_STALE_SECONDS)
    with _connection() as conn:
        cursor = conn.execute(
            """
            UPDATE download_generations
            SET status='failed', finished_at=?
            WHERE status='generating' AND created_at<?
            """,
            (_iso(now), _iso(cutoff)),
        )
    return cursor.rowcount


def check_download_generation_limits(owner_key: str, report_kind: str = "ticker") -> None:
    """Raise ActiveJobError or DailyLimitError if the user cannot start a download.

    Limits (all configurable via environment variables):
    - Per-user active downloads: REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER (default 1)
    - Per-user daily downloads:  REPORT_DOWNLOAD_DAILY_LIMIT_PER_USER (default 5)
    - Global active downloads:   REPORT_DOWNLOAD_MAX_GLOBAL_ACTIVE (default 3)
    """
    owner_key = str(owner_key or "").strip()
    if not owner_key:
        raise ValueError("A signed-in account is required to generate download reports.")

    cleanup_stale_download_generations()

    report_kind = str(report_kind or "ticker").strip().lower()
    max_per_user_env = "PORTFOLIO_DOWNLOAD_MAX_ACTIVE_PER_USER" if report_kind == "portfolio" else "REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER"
    daily_limit_env = "PORTFOLIO_DOWNLOAD_DAILY_LIMIT_PER_USER" if report_kind == "portfolio" else "REPORT_DOWNLOAD_DAILY_LIMIT_PER_USER"
    max_per_user = max(1, int(os.environ.get(max_per_user_env, os.environ.get("REPORT_DOWNLOAD_MAX_ACTIVE_PER_USER", "1"))))
    daily_limit = max(1, int(os.environ.get(daily_limit_env, os.environ.get("REPORT_DOWNLOAD_DAILY_LIMIT_PER_USER", "5"))))
    max_global = max(1, int(os.environ.get("REPORT_DOWNLOAD_MAX_GLOBAL_ACTIVE", "3")))

    now = _now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    init_job_db()
    with _connection() as conn:
        user_active = conn.execute(
            "SELECT COUNT(*) FROM download_generations WHERE owner_key=? AND status='generating'",
            (owner_key,),
        ).fetchone()[0]
        if user_active >= max_per_user:
            raise ActiveJobError(
                f"You already have a download report generating. "
                f"Max {max_per_user} concurrent per account."
            )

        global_active = conn.execute(
            "SELECT COUNT(*) FROM download_generations WHERE status='generating'",
        ).fetchone()[0]
        if global_active >= max_global:
            raise ActiveJobError(
                f"The server is currently generating the maximum number of reports ({max_global}). "
                f"Please try again in a few minutes."
            )

        today_count = conn.execute(
            "SELECT COUNT(*) FROM download_generations WHERE owner_key=? AND created_at>=?",
            (owner_key, _iso(day_start)),
        ).fetchone()[0]
        if today_count >= daily_limit:
            raise DailyLimitError(
                f"Daily download report limit reached ({daily_limit} per account)."
            )


def start_download_generation(owner_key: str, ticker: str, report_kind: str = "ticker") -> str:
    """Record the start of a synchronous download generation and return its session ID."""
    owner_key = str(owner_key or "").strip()
    ticker = str(ticker or "").strip().upper()
    if not owner_key:
        raise ValueError("A signed-in account is required to generate download reports.")
    session_id = uuid.uuid4().hex
    now = _now()
    init_job_db()
    with _connection() as conn:
        conn.execute(
            """
            INSERT INTO download_generations (id, owner_key, ticker, status, created_at)
            VALUES (?, ?, ?, 'generating', ?)
            """,
            (session_id, owner_key, ticker, _iso(now)),
        )
    return session_id


def finish_download_generation(session_id: str, *, success: bool = True) -> None:
    """Mark a download generation as done or failed."""
    status = "done" if success else "failed"
    now = _now()
    init_job_db()
    with _connection() as conn:
        row = conn.execute(
            "SELECT created_at FROM download_generations WHERE id=?",
            (session_id,),
        ).fetchone()
        elapsed = None
        if row is not None:
            try:
                created = dt.datetime.fromisoformat(row["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=dt.timezone.utc)
                elapsed = (now - created).total_seconds()
            except (ValueError, TypeError):
                pass
        conn.execute(
            """
            UPDATE download_generations
            SET status=?, finished_at=?, elapsed=?
            WHERE id=?
            """,
            (status, _iso(now), elapsed, session_id),
        )
