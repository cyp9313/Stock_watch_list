from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
import time

# Ensure project root is importable for config_loader
APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config_loader import load_project_env

from .jobs import (
    claim_next_job,
    expire_stale_queued_jobs,
    mark_email_sent,
    mark_job_failure,
    mark_job_sent,
    materialize_due_schedules,
    prune_old_jobs,
    recover_interrupted_jobs,
    store_generated_report,
)
from .mailer import compute_job_message_id, send_report_email
from .service import generate_report, _get_market_date


def process_one_job() -> bool:
    job = claim_next_job()
    if not job:
        return False

    job_id = job["id"]
    ticker = job["ticker"]
    print(
        f"[ReportWorker] Job {job_id[:8]} started: ticker={ticker}, "
        f"recipient={job['recipient_masked']}, attempt={job['attempts']}/{job['max_attempts']}",
        flush=True,
    )
    try:
        html_bytes = job.get("report_html")
        file_name = job.get("file_name")
        if html_bytes is None:
            result = generate_report(
                ticker,
                user_scope=job["owner_key"],
                months=int(job["months"]),
                search_provider=job["search_provider"],
                no_article_fetch=bool(job["no_article_fetch"]),
            )
            if not result.get("success"):
                detail = result.get("stderr") or result.get("stdout") or result.get("error")
                raise RuntimeError(detail or "Daily report generation failed.")
            html_bytes = result["html_bytes"]
            file_name = result["file_name"]
            job["report_date"] = result["report_date"]
            store_generated_report(
                job_id,
                html_bytes,
                file_name,
                result["report_date"],
                result.get("elapsed", 0),
            )
            print(
                f"[ReportWorker] Job {job_id[:8]} generated in {result.get('elapsed', 0):.1f}s; sending email",
                flush=True,
            )

        # Idempotent email delivery: if the SMTP send already succeeded
        # (crash between send and mark_job_sent), skip re-sending.
        if job.get("email_sent_at"):
            print(
                f"[ReportWorker] Job {job_id[:8]} email already sent at "
                f"{job['email_sent_at']}; confirming delivery without re-send",
                flush=True,
            )
            mark_job_sent(job_id)
            print(f"[ReportWorker] Job {job_id[:8]} confirmed as sent", flush=True)
        else:
            message_id = compute_job_message_id(job_id)
            send_report_email(
                recipient=job["recipient_email"],
                ticker=ticker,
                report_date=job.get("report_date") or _get_market_date(),
                file_name=file_name,
                html_bytes=bytes(html_bytes),
                message_id=message_id,
            )
            mark_email_sent(job_id, message_id)
            mark_job_sent(job_id)
            print(f"[ReportWorker] Job {job_id[:8]} sent successfully", flush=True)
    except Exception as exc:
        next_status = mark_job_failure(job_id, f"{type(exc).__name__}: {exc}")
        print(
            f"[ReportWorker] Job {job_id[:8]} failed; status={next_status}: {type(exc).__name__}: {exc}",
            flush=True,
        )
    return True


def main() -> None:
    load_project_env()
    recovered = recover_interrupted_jobs()
    if recovered:
        print(f"[ReportWorker] Recovered {recovered} interrupted job(s)", flush=True)
    scheduled = materialize_due_schedules()
    if scheduled:
        print(f"[ReportWorker] Queued {scheduled} due weekly schedule(s)", flush=True)
    print("[ReportWorker] AI daily report email worker started", flush=True)

    poll_seconds = max(1, int(os.environ.get("REPORT_WORKER_POLL_SECONDS", "3")))
    last_prune = 0.0
    try:
        while True:
            expired = expire_stale_queued_jobs()
            if expired:
                print(f"[ReportWorker] Expired {expired} stale queued job(s)", flush=True)
            scheduled = materialize_due_schedules()
            if scheduled:
                print(f"[ReportWorker] Queued {scheduled} due weekly schedule(s)", flush=True)
            worked = process_one_job()
            now = time.monotonic()
            if now - last_prune >= 3600:
                prune_old_jobs()
                last_prune = now
            if not worked:
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("[ReportWorker] Worker stopped", flush=True)


if __name__ == "__main__":
    main()
