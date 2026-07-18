from __future__ import annotations

import datetime as dt

from daily_report import jobs


def test_enqueue_portfolio_job_sets_generic_subject_fields(temp_db_path, monkeypatch):
    jobs._INITIALIZED_DATABASES.discard(str(jobs._db_path()))
    job = jobs.enqueue_portfolio_email_job(
        owner_key="alice",
        portfolio_page_id="pf_abc",
        portfolio_name="Growth Portfolio",
        recipient_email="alice@example.com",
        search_provider="auto",
        settings={"base_currency": "EUR"},
    )

    assert job["report_kind"] == "portfolio"
    assert job["subject_key"] == "pf_abc"
    assert job["subject_name"] == "Growth Portfolio"
    assert job["ticker"] == "Growth Portfolio"


def test_portfolio_schedule_materializes_portfolio_job(temp_db_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_MAX_SCHEDULES_PER_USER", "5")
    jobs._INITIALIZED_DATABASES.discard(str(jobs._db_path()))
    schedule = jobs.create_weekly_portfolio_schedule(
        owner_key="alice",
        portfolio_page_id="pf_abc",
        portfolio_name="Growth Portfolio",
        recipient_email="alice@example.com",
        weekdays=[0],
        local_time="18:00",
    )
    due_at = dt.datetime(2026, 7, 6, 16, 0, tzinfo=dt.timezone.utc)
    with jobs._connection() as conn:
        conn.execute("UPDATE report_schedules SET next_run_at=? WHERE id=?", ((due_at - dt.timedelta(minutes=1)).isoformat(), schedule["id"]))

    assert jobs.materialize_due_schedules(now_utc=due_at) == 1
    with jobs._connection() as conn:
        row = conn.execute("SELECT report_kind, subject_key, subject_name FROM report_jobs").fetchone()

    assert row["report_kind"] == "portfolio"
    assert row["subject_key"] == "pf_abc"
    assert row["subject_name"] == "Growth Portfolio"
