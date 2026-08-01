"""Per-email decision-dashboard preference coverage."""

from __future__ import annotations

import json

from daily_report import jobs, worker


def _initialize_temp_jobs_db() -> None:
    jobs._INITIALIZED_DATABASES.discard(str(jobs._db_path()))
    jobs.init_job_db()


def test_one_time_email_persists_choice_and_worker_forwards_it(temp_db_path, monkeypatch) -> None:
    _initialize_temp_jobs_db()
    job = jobs.enqueue_email_job(
        owner_key="alice",
        ticker="AAPL",
        recipient_email="alice@example.com",
        decision_dashboard=False,
    )
    assert json.loads(job["payload_json"])["decision_dashboard"] is False
    worker_job = jobs.claim_next_job()
    assert worker_job is not None

    captured = {}

    def fake_generate_report(*_args, **kwargs):
        captured.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(worker, "generate_report", fake_generate_report)
    assert worker.generate_job_report(worker_job)["success"] is True
    assert captured["decision_dashboard"] is False


def test_existing_email_jobs_default_to_dashboard_enabled(monkeypatch) -> None:
    captured = {}
    job = {
        "report_kind": "ticker", "subject_key": "AAPL", "ticker": "AAPL",
        "owner_key": "alice", "months": 3, "search_provider": "auto",
        "no_article_fetch": 0, "payload_json": "{}",
    }

    monkeypatch.setattr(worker, "generate_report", lambda *_args, **kwargs: captured.update(kwargs) or {"success": True})
    worker.generate_job_report(job)
    assert captured["decision_dashboard"] is True


def test_weekly_schedule_persists_dashboard_choice(temp_db_path) -> None:
    _initialize_temp_jobs_db()
    schedule = jobs.create_weekly_schedule(
        owner_key="alice",
        ticker="AAPL",
        recipient_email="alice@example.com",
        weekday=0,
        local_time="18:00",
        decision_dashboard=False,
    )
    assert json.loads(schedule["payload_json"])["decision_dashboard"] is False
