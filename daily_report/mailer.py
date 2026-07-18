from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
import os
import smtplib
import ssl


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    authorization_code: str
    sender: str
    use_ssl: bool
    timeout: int


def load_smtp_config() -> SmtpConfig:
    username = os.environ.get("REPORT_SMTP_USER", "").strip()
    authorization_code = os.environ.get("REPORT_SMTP_AUTH_CODE", "").strip()
    sender = os.environ.get("REPORT_SMTP_FROM", "").strip() or username
    missing = [
        name
        for name, value in (
            ("REPORT_SMTP_USER", username),
            ("REPORT_SMTP_AUTH_CODE", authorization_code),
            ("REPORT_SMTP_FROM", sender),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing SMTP configuration: {', '.join(missing)}")
    return SmtpConfig(
        host=os.environ.get("REPORT_SMTP_HOST", "smtp.163.com").strip(),
        port=int(os.environ.get("REPORT_SMTP_PORT", "465")),
        username=username,
        authorization_code=authorization_code,
        sender=sender,
        use_ssl=os.environ.get("REPORT_SMTP_USE_SSL", "true").strip().lower() not in {"0", "false", "no"},
        timeout=max(5, int(os.environ.get("REPORT_SMTP_TIMEOUT", "30"))),
    )


def smtp_configured() -> bool:
    try:
        load_smtp_config()
        return True
    except (RuntimeError, ValueError):
        return False


def compute_job_message_id(job_id: str) -> str:
    """Return a deterministic RFC 5322 Message-ID derived from *job_id*.

    Using a stable Message-ID means that even if the worker re-sends the
    same job after a crash, mail servers and clients can deduplicate by
    Message-ID header.
    """
    config = load_smtp_config()
    domain = config.sender.split("@")[-1] if "@" in config.sender else "localhost"
    return f"<job-{job_id}@{domain}>"


def send_report_email(
    *,
    recipient: str,
    ticker: str | None = None,
    report_title: str | None = None,
    report_date: str,
    file_name: str,
    html_bytes: bytes,
    report_kind: str = "ticker",
    message_id: str | None = None,
) -> None:
    config = load_smtp_config()
    message = EmailMessage()
    message["From"] = formataddr(("Stock Watchlist AI Agent", config.sender))
    message["To"] = recipient
    subject_name = report_title or ticker or "Portfolio"
    if report_kind == "portfolio":
        message["Subject"] = f"{subject_name} AI Portfolio Report - {report_date}"
        body = (
            f"Your AI portfolio analysis report for {subject_name} is attached.\n\n"
            "This report is for research purposes only and is not investment advice."
        )
    else:
        message["Subject"] = f"{subject_name} AI Stock Daily Report - {report_date}"
        body = (
            f"Your AI Agent daily report for {subject_name} is attached.\n\n"
            "This report is for research purposes only and is not investment advice."
        )
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id or make_msgid(domain=config.sender.split("@")[-1])
    message.set_content(body)
    message.add_attachment(
        html_bytes,
        maintype="text",
        subtype="html",
        filename=file_name,
    )

    context = ssl.create_default_context()
    if config.use_ssl:
        with smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout, context=context) as smtp:
            smtp.login(config.username, config.authorization_code)
            smtp.send_message(message)
        return

    with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(config.username, config.authorization_code)
        smtp.send_message(message)

