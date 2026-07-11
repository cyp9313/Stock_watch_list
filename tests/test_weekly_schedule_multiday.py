"""Tests for one-ticker weekly schedules with multiple selected weekdays."""

from __future__ import annotations

import datetime as dt

import pytest

from daily_report import jobs


@pytest.fixture
def schedule_db(temp_db_path, monkeypatch):
    monkeypatch.setenv("REPORT_MAX_SCHEDULES_PER_USER", "7")
    jobs._INITIALIZED_DATABASES.discard(str(jobs._db_path()))
    jobs.init_job_db()
    yield
    jobs._INITIALIZED_DATABASES.discard(str(jobs._db_path()))


def _create_schedule(*, ticker="AAPL", weekdays=None, weekday=None):
    kwargs = {
        "owner_key": "alice",
        "ticker": ticker,
        "recipient_email": "alice@example.com",
        "local_time": "18:00",
    }
    if weekdays is not None:
        kwargs["weekdays"] = weekdays
    if weekday is not None:
        kwargs["weekday"] = weekday
    return jobs.create_weekly_schedule(**kwargs)


def test_one_ticker_schedule_stores_all_selected_weekdays(schedule_db):
    schedule = _create_schedule(weekdays=[4, 0, 2, 2])

    assert schedule["ticker"] == "AAPL"
    assert schedule["weekdays"] == [0, 2, 4]
    assert schedule["weekday"] == 0  # legacy compatibility column
    assert len(jobs.list_owner_schedules("alice")) == 1


def test_legacy_single_weekday_caller_remains_compatible(schedule_db):
    schedule = _create_schedule(ticker="MSFT", weekday=1)

    assert schedule["weekdays"] == [1]


def test_existing_single_day_row_is_backfilled_on_database_initialization(schedule_db):
    schedule = _create_schedule(ticker="LEGACY", weekday=3)
    with jobs._connection() as conn:
        conn.execute(
            "UPDATE report_schedules SET weekdays_json=NULL WHERE id=?",
            (schedule["id"],),
        )

    jobs._INITIALIZED_DATABASES.discard(str(jobs._db_path()))
    jobs.init_job_db()

    restored = jobs.get_schedule(schedule["id"], owner_key="alice")
    assert restored is not None
    assert restored["weekdays"] == [3]


def test_schedule_requires_at_least_one_weekday(schedule_db):
    with pytest.raises(ValueError, match="weekday"):
        _create_schedule(weekdays=[])


def test_user_can_create_at_most_seven_ticker_schedules(schedule_db):
    for index in range(7):
        _create_schedule(ticker=f"TICK{index}", weekdays=[index])

    with pytest.raises(jobs.ScheduleLimitError, match="7"):
        _create_schedule(ticker="TICK7", weekdays=[0])


def test_multiday_schedule_materializes_then_advances_to_next_selected_day(schedule_db):
    schedule = _create_schedule(weekdays=[0, 2])
    due_at = dt.datetime(2026, 7, 6, 16, 0, tzinfo=dt.timezone.utc)
    with jobs._connection() as conn:
        conn.execute(
            "UPDATE report_schedules SET next_run_at=? WHERE id=?",
            ((due_at - dt.timedelta(minutes=1)).isoformat(), schedule["id"]),
        )

    created = jobs.materialize_due_schedules(now_utc=due_at)
    updated = jobs.get_schedule(schedule["id"], owner_key="alice")

    assert created == 1
    assert updated is not None
    assert updated["weekdays"] == [0, 2]
    next_run = dt.datetime.fromisoformat(updated["next_run_at"])
    assert next_run.astimezone(dt.timezone.utc) > due_at
