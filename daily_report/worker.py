from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import time

from dotenv import load_dotenv

from .jobs import (
    claim_next_job,
    mark_job_failure,
    mark_job_sent,
    materialize_due_schedules,
    prune_old_jobs,
    recover_interrupted_jobs,
    store_generated_report,
)
from .mailer import send_report_email
from .service import generate_report


APP_ROOT = Path(__file__).resolve().parent.parent


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

        send_report_email(
            recipient=job["recipient_email"],
            ticker=ticker,
            report_date=job.get("report_date") or dt.date.today().isoformat(),
            file_name=file_name,
            html_bytes=bytes(html_bytes),
        )
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
    load_dotenv(APP_ROOT / ".env")
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
