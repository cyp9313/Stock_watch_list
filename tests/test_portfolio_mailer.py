from __future__ import annotations

from email.message import EmailMessage

from daily_report import mailer


class DummySMTP:
    sent_messages: list[EmailMessage] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, username, password):
        return None

    def send_message(self, message):
        self.sent_messages.append(message)


def test_portfolio_email_subject_and_body(monkeypatch):
    DummySMTP.sent_messages.clear()
    monkeypatch.setenv("REPORT_SMTP_USER", "bot@example.com")
    monkeypatch.setenv("REPORT_SMTP_AUTH_CODE", "secret")
    monkeypatch.setenv("REPORT_SMTP_FROM", "bot@example.com")
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", DummySMTP)

    mailer.send_report_email(
        recipient="alice@example.com",
        report_title="Growth Portfolio",
        report_kind="portfolio",
        report_date="2026-07-17",
        file_name="report.html",
        html_bytes=b"<html></html>",
        message_id="<job-test@example.com>",
    )

    message = DummySMTP.sent_messages[0]
    assert message["Subject"] == "Growth Portfolio AI Portfolio Report - 2026-07-17"
    assert "portfolio analysis report" in message.get_body(preferencelist=("plain",)).get_content()
