from __future__ import annotations

from daily_report import worker


def _telegram_job(kind: str) -> dict:
    return {
        "id": "job-1", "report_kind": kind, "subject_name": "NVDA", "ticker": "NVDA",
        "recipient_masked": "Telegram", "attempts": 1, "max_attempts": 3,
    }


def test_stock_worker_delivers_complete_html_with_summary(monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(worker, "claim_next_job", lambda: _telegram_job("telegram_ticker"))
    monkeypatch.setattr(worker, "generate_job_report", lambda _job: {
        "success": True, "telegram_messages": ["summary"], "html_bytes": b"<html>complete</html>",
        "file_name": "NVDA_report.html", "elapsed": 1.0,
    })
    monkeypatch.setattr(worker, "deliver_telegram_job", lambda _job, messages, **kwargs: sent.append((messages, kwargs)))
    monkeypatch.setattr(worker, "mark_job_sent", lambda job_id: sent.append(("sent", job_id)))

    assert worker.process_one_job() is True
    assert sent == [(["summary"], {"html_attachment": ("NVDA_report.html", b"<html>complete</html>")}), ("sent", "job-1")]


def test_daily_account_brief_stays_text_only(monkeypatch) -> None:
    sent: list[tuple] = []
    monkeypatch.setattr(worker, "claim_next_job", lambda: _telegram_job("telegram_daily_account_brief"))
    monkeypatch.setattr(worker, "generate_job_report", lambda _job: {"success": True, "telegram_messages": ["brief"], "elapsed": 0.0})
    monkeypatch.setattr(worker, "deliver_telegram_job", lambda _job, messages, **kwargs: sent.append((messages, kwargs)))
    monkeypatch.setattr(worker, "mark_job_sent", lambda _job_id: None)

    assert worker.process_one_job() is True
    assert sent == [(["brief"], {"html_attachment": None})]


def test_missing_html_fails_stock_job_and_notifies_chat(monkeypatch) -> None:
    outcomes: list[str] = []
    monkeypatch.setattr(worker, "claim_next_job", lambda: _telegram_job("telegram_ticker"))
    monkeypatch.setattr(worker, "generate_job_report", lambda _job: {"success": True, "telegram_messages": ["summary"], "elapsed": 1.0})
    monkeypatch.setattr(worker, "mark_job_failure", lambda _job_id, _error, **_kwargs: "failed")
    monkeypatch.setattr(worker, "mark_telegram_job_failure", lambda _job, _error: outcomes.append("failed"))
    monkeypatch.setattr(worker, "notify_telegram_report_failure", lambda _job: outcomes.append("notified"))

    assert worker.process_one_job() is True
    assert outcomes == ["failed", "notified"]
