"""Telegram command bridge for account-scoped AI stock reports.

The bot worker only receives commands and queues work.  The existing report
worker remains the sole process that runs the expensive research pipeline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from cryptography.fernet import Fernet, InvalidToken

from daily_report import jobs


BOT_API = "https://api.telegram.org/bot{token}/{method}"
_TICKER_RE = re.compile(r"^[A-Za-z0-9.^=\-]{1,24}$")


class TelegramAIReportError(RuntimeError):
    """A safe, user-presentable Telegram integration error."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _fernet() -> Fernet:
    value = (
        os.environ.get("TELEGRAM_AI_REPORT_ENCRYPTION_KEY", "").strip()
        or os.environ.get("TRADING212_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    )
    if not value:
        raise TelegramAIReportError(
            "TELEGRAM_AI_REPORT_ENCRYPTION_KEY is not configured; the bot token cannot be stored safely."
        )
    try:
        return Fernet(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise TelegramAIReportError("TELEGRAM_AI_REPORT_ENCRYPTION_KEY is invalid.") from exc


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise TelegramAIReportError("The stored Telegram bot token cannot be decrypted with the configured key.") from exc


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def init_telegram_ai_report_db() -> None:
    jobs.init_job_db()
    with jobs._connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_ai_bot_settings (
                user_id INTEGER PRIMARY KEY,
                owner_key TEXT NOT NULL,
                token_encrypted TEXT NOT NULL,
                token_fingerprint TEXT NOT NULL UNIQUE,
                bot_username TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                polling_offset INTEGER NOT NULL DEFAULT 0,
                link_code_hash TEXT,
                link_code_expires_at TEXT,
                last_polled_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(telegram_ai_bot_settings)")}
        if "owner_key" not in columns:
            conn.execute("ALTER TABLE telegram_ai_bot_settings ADD COLUMN owner_key TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_ai_bot_chats (
                user_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                chat_title TEXT,
                authorized_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (user_id, chat_id),
                FOREIGN KEY (user_id) REFERENCES telegram_ai_bot_settings(user_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_ai_report_requests (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                job_id TEXT,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, chat_id, message_id)
            )
            """
        )
        request_columns = {row[1] for row in conn.execute("PRAGMA table_info(telegram_ai_report_requests)")}
        if "request_kind" not in request_columns:
            conn.execute("ALTER TABLE telegram_ai_report_requests ADD COLUMN request_kind TEXT NOT NULL DEFAULT 'ticker'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_ai_consumed_updates (
                user_id INTEGER NOT NULL,
                update_id INTEGER NOT NULL,
                consumed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, update_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_telegram_ai_bot_enabled ON telegram_ai_bot_settings(enabled)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_telegram_ai_requests_user_created ON telegram_ai_report_requests(user_id, created_at DESC)")


def _telegram_api(token: str, method: str, *, payload: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    try:
        response = requests.post(BOT_API.format(token=token, method=method), json=payload or {}, timeout=timeout)
        parsed = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise TelegramAIReportError(f"Telegram API request failed: {type(exc).__name__}.") from exc
    if not response.ok or not isinstance(parsed, dict) or not parsed.get("ok"):
        detail = str(parsed.get("description") if isinstance(parsed, dict) else "unknown error")[:240]
        raise TelegramAIReportError(f"Telegram API rejected the request: {detail}")
    return parsed


def _validate_token(token: str) -> tuple[str, str | None]:
    result = _telegram_api(token, "getMe")
    bot = result.get("result") if isinstance(result.get("result"), dict) else {}
    if not bot.get("is_bot") or not bot.get("id"):
        raise TelegramAIReportError("Telegram did not identify these credentials as a bot token.")
    username = str(bot.get("username") or "").strip() or None
    webhook = _telegram_api(token, "getWebhookInfo").get("result")
    if isinstance(webhook, dict) and str(webhook.get("url") or "").strip():
        raise TelegramAIReportError(
            "This bot has an active webhook. Remove it first because getUpdates long polling and webhooks cannot run together."
        )
    return str(bot.get("id")), username


def _discard_pending_updates(token: str) -> tuple[int, int]:
    """Advance a newly connected bot past messages sent before setup.

    Position-alert bots may already have a long chat history.  Those old
    `/link` messages are not fresh pairing attempts and must never trigger a
    burst of expired-code replies when command polling is first enabled.
    """
    next_offset = 0
    discarded = 0
    for _page in range(100):
        payload: dict[str, Any] = {"timeout": 0, "limit": 100, "allowed_updates": ["message"]}
        if next_offset:
            payload["offset"] = next_offset
        result = _telegram_api(token, "getUpdates", payload=payload, timeout=20)
        updates = result.get("result") if isinstance(result.get("result"), list) else []
        if not updates:
            break
        page_count = 0
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = max(next_offset, update_id + 1)
                page_count += 1
        discarded += page_count
        if len(updates) < 100 or page_count == 0:
            break
    return next_offset, discarded


def get_telegram_ai_bot_status(user_id: int) -> dict[str, Any]:
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        settings = _row_dict(conn.execute("SELECT * FROM telegram_ai_bot_settings WHERE user_id=?", (int(user_id),)).fetchone())
        chats = [dict(row) for row in conn.execute(
            "SELECT chat_id, chat_type, chat_title, authorized_at, last_seen_at FROM telegram_ai_bot_chats WHERE user_id=? ORDER BY authorized_at DESC",
            (int(user_id),),
        ).fetchall()]
        requests_rows = [dict(row) for row in conn.execute(
            "SELECT ticker, request_kind, status, error, created_at, updated_at FROM telegram_ai_report_requests WHERE user_id=? ORDER BY created_at DESC LIMIT 8",
            (int(user_id),),
        ).fetchall()]
    if not settings:
        return {"configured": False, "chats": chats, "recent_requests": requests_rows}
    expiry = settings.get("link_code_expires_at")
    return {
        "configured": True,
        "enabled": bool(settings.get("enabled")),
        "bot_username": settings.get("bot_username"),
        "last_polled_at": settings.get("last_polled_at"),
        "last_error": settings.get("last_error"),
        "link_code_expires_at": expiry,
        "chats": chats,
        "recent_requests": requests_rows,
    }


def save_telegram_ai_bot_settings(*, user_id: int, owner_key: str, bot_token: str | None, enabled: bool) -> dict[str, Any]:
    """Validate and safely persist one account's bot token.

    An empty token retains a previously saved token, allowing the enabled state
    to be changed without revealing the secret back to the browser.
    """
    user_id = int(user_id)
    owner_key = str(owner_key or "").strip()
    if not owner_key:
        raise TelegramAIReportError("A signed-in account is required.")
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        current = _row_dict(conn.execute("SELECT * FROM telegram_ai_bot_settings WHERE user_id=?", (user_id,)).fetchone())
    supplied = str(bot_token or "").strip()
    if not supplied:
        if not current:
            raise TelegramAIReportError("Enter a Telegram bot token before saving.")
        supplied = _decrypt(str(current["token_encrypted"]))
    _, username = _validate_token(supplied)
    fingerprint = _fingerprint(supplied)
    now = _iso()
    initial_offset = 0
    discarded_updates = 0
    # Do this before saving/enabling the new token.  A token change has no
    # active command poller for the new token, so there is no competing
    # getUpdates request to race with this initial cursor synchronization.
    if current is None or current.get("token_fingerprint") != fingerprint:
        initial_offset, discarded_updates = _discard_pending_updates(supplied)
    with jobs._connection() as conn:
        conflict = conn.execute(
            "SELECT user_id FROM telegram_ai_bot_settings WHERE token_fingerprint=? AND user_id<>?",
            (fingerprint, user_id),
        ).fetchone()
        if conflict:
            raise TelegramAIReportError("This Telegram bot is already connected to a different website account.")
        previous = _row_dict(conn.execute("SELECT * FROM telegram_ai_bot_settings WHERE user_id=?", (user_id,)).fetchone())
        token_changed = bool(previous and previous.get("token_fingerprint") != fingerprint)
        if previous:
            conn.execute(
                """UPDATE telegram_ai_bot_settings SET owner_key=?, token_encrypted=?, token_fingerprint=?, bot_username=?, enabled=?,
                   polling_offset=CASE WHEN ? THEN ? ELSE polling_offset END, link_code_hash=NULL, link_code_expires_at=NULL,
                   last_error=NULL, updated_at=? WHERE user_id=?""",
                (owner_key, _encrypt(supplied), fingerprint, username, int(bool(enabled)), int(token_changed), initial_offset, now, user_id),
            )
            if token_changed:
                conn.execute("DELETE FROM telegram_ai_bot_chats WHERE user_id=?", (user_id,))
        else:
            conn.execute(
                """INSERT INTO telegram_ai_bot_settings
                   (user_id, owner_key, token_encrypted, token_fingerprint, bot_username, enabled, polling_offset, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, owner_key, _encrypt(supplied), fingerprint, username, int(bool(enabled)), initial_offset, now, now),
            )
    status = get_telegram_ai_bot_status(user_id)
    status["discarded_pending_updates"] = discarded_updates
    return status


def remove_telegram_ai_bot_settings(user_id: int) -> None:
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        conn.execute("DELETE FROM telegram_ai_bot_chats WHERE user_id=?", (int(user_id),))
        conn.execute("DELETE FROM telegram_ai_bot_settings WHERE user_id=?", (int(user_id),))


def discard_telegram_ai_bot_backlog(user_id: int) -> int:
    """Ignore queued pre-existing Bot messages, preserving current pairings."""
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        row = _row_dict(conn.execute(
            "SELECT token_encrypted FROM telegram_ai_bot_settings WHERE user_id=?", (int(user_id),)
        ).fetchone())
    if not row:
        raise TelegramAIReportError("Save a Telegram bot token first.")
    offset, discarded = _discard_pending_updates(_decrypt(str(row["token_encrypted"])))
    with jobs._connection() as conn:
        conn.execute(
            "UPDATE telegram_ai_bot_settings SET polling_offset=?, last_error=NULL, updated_at=? WHERE user_id=?",
            (offset, _iso(), int(user_id)),
        )
    return discarded


def create_link_code(user_id: int, *, ttl_seconds: int | None = None) -> dict[str, str]:
    init_telegram_ai_report_db()
    ttl = max(60, min(int(ttl_seconds or os.environ.get("TELEGRAM_AI_REPORT_LINK_TTL_SECONDS", "900")), 3600))
    code = secrets.token_urlsafe(12)
    expires_at = _now() + timedelta(seconds=ttl)
    with jobs._connection() as conn:
        row = conn.execute("SELECT user_id FROM telegram_ai_bot_settings WHERE user_id=?", (int(user_id),)).fetchone()
        if not row:
            raise TelegramAIReportError("Save a Telegram bot token before creating a pairing code.")
        conn.execute(
            "UPDATE telegram_ai_bot_settings SET link_code_hash=?, link_code_expires_at=?, updated_at=? WHERE user_id=?",
            (hashlib.sha256(code.encode("utf-8")).hexdigest(), _iso(expires_at), _iso(), int(user_id)),
        )
    return {"code": code, "expires_at": _iso(expires_at)}


def revoke_telegram_chat(user_id: int, chat_id: str) -> None:
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        conn.execute("DELETE FROM telegram_ai_bot_chats WHERE user_id=? AND chat_id=?", (int(user_id), str(chat_id)))


def _enabled_bots() -> list[dict[str, Any]]:
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM telegram_ai_bot_settings WHERE enabled=1").fetchall()]
    bots: list[dict[str, Any]] = []
    for row in rows:
        try:
            row["token"] = _decrypt(str(row["token_encrypted"]))
        except TelegramAIReportError as exc:
            _record_poll_error(int(row["user_id"]), str(exc))
            continue
        bots.append(row)
    return bots


def _record_poll_error(user_id: int, error: str | None) -> None:
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        conn.execute(
            "UPDATE telegram_ai_bot_settings SET last_error=?, updated_at=? WHERE user_id=?",
            ((str(error)[:500] if error else None), _iso(), int(user_id)),
        )


def _claim_update(user_id: int, update_id: int) -> bool:
    """Return true only for the first processing of an inbound update ID."""
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO telegram_ai_consumed_updates(user_id, update_id, consumed_at)
               VALUES (?, ?, ?)""",
            (int(user_id), int(update_id), _iso()),
        )
    return cursor.rowcount == 1


def _send(token: str, chat_id: str, text: str, *, reply_to_message_id: int | None = None) -> None:
    payload: dict[str, Any] = {
        "chat_id": str(chat_id), "text": str(text)[:4096], "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = int(reply_to_message_id)
    _telegram_api(token, "sendMessage", payload=payload)


def _command(text: str) -> tuple[str, str | None]:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return "", None
    match = re.fullmatch(r"/link(?:@\w+)?\s+([A-Za-z0-9_-]{8,80})", cleaned, flags=re.I)
    if match:
        return "link", match.group(1)
    match = re.fullmatch(r"/(?:ai|report)(?:@\w+)?\s+([A-Za-z0-9.^=\-]{1,24})", cleaned, flags=re.I)
    if not match:
        match = re.fullmatch(r"@AI\s+([A-Za-z0-9.^=\-]{1,24})", cleaned, flags=re.I)
    if match:
        argument = match.group(1).upper()
        return ("daily_account_brief", None) if argument == "DAB" else ("report", argument)
    if re.fullmatch(r"/(?:help|start)(?:@\w+)?", cleaned, flags=re.I):
        return "help", None
    return "", None


def _chat_authorized(conn: sqlite3.Connection, user_id: int, chat_id: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM telegram_ai_bot_chats WHERE user_id=? AND chat_id=?", (user_id, str(chat_id))
    ).fetchone())


def _link_chat(bot: dict[str, Any], chat: dict[str, Any], code: str) -> bool:
    user_id = int(bot["user_id"])
    with jobs._connection() as conn:
        current = _row_dict(conn.execute("SELECT link_code_hash, link_code_expires_at FROM telegram_ai_bot_settings WHERE user_id=?", (user_id,)).fetchone())
        expiry_text = str((current or {}).get("link_code_expires_at") or "")
        expected = str((current or {}).get("link_code_hash") or "")
        try:
            valid_until = datetime.fromisoformat(expiry_text)
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)
        except ValueError:
            valid_until = _now() - timedelta(seconds=1)
        actual = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if not expected or not hmac.compare_digest(expected, actual) or valid_until <= _now():
            return False
        conn.execute(
            """INSERT INTO telegram_ai_bot_chats (user_id, chat_id, chat_type, chat_title, authorized_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, chat_id) DO UPDATE SET chat_type=excluded.chat_type, chat_title=excluded.chat_title,
               last_seen_at=excluded.last_seen_at""",
            (user_id, str(chat["id"]), str(chat.get("type") or "unknown"), str(chat.get("title") or chat.get("username") or "")[:180], _iso(), _iso()),
        )
        conn.execute("UPDATE telegram_ai_bot_settings SET link_code_hash=NULL, link_code_expires_at=NULL, updated_at=? WHERE user_id=?", (_iso(), user_id))
    return True


def _record_queued_request(*, user_id: int, chat_id: str, message_id: int, ticker: str, request_kind: str, job_id: str) -> None:
    with jobs._connection() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO telegram_ai_report_requests
               (id, user_id, chat_id, message_id, ticker, request_kind, job_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (secrets.token_hex(16), int(user_id), str(chat_id), int(message_id), ticker, request_kind, job_id, _iso(), _iso()),
        )


def _queue_report(bot: dict[str, Any], chat_id: str, message_id: int, ticker: str) -> str:
    if not _TICKER_RE.fullmatch(ticker):
        raise TelegramAIReportError("Ticker format is not supported.")
    job = jobs.enqueue_telegram_ticker_job(
        owner_key=str(bot.get("owner_key") or ""), user_id=int(bot["user_id"]), chat_id=str(chat_id),
        reply_to_message_id=message_id, ticker=ticker, decision_dashboard=True,
    )
    _record_queued_request(user_id=int(bot["user_id"]), chat_id=chat_id, message_id=message_id,
                           ticker=ticker, request_kind="ticker", job_id=str(job["id"]))
    return str(job["id"])


def _queue_daily_account_brief(bot: dict[str, Any], chat_id: str, message_id: int) -> str:
    job = jobs.enqueue_telegram_daily_account_brief_job(
        owner_key=str(bot.get("owner_key") or ""), user_id=int(bot["user_id"]), chat_id=str(chat_id),
        reply_to_message_id=message_id,
    )
    _record_queued_request(user_id=int(bot["user_id"]), chat_id=chat_id, message_id=message_id,
                           ticker="DAB", request_kind="daily_account_brief", job_id=str(job["id"]))
    return str(job["id"])


def _process_update(bot: dict[str, Any], update: dict[str, Any]) -> None:
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
        return
    chat = message["chat"]
    chat_id = str(chat.get("id") or "")
    text = message.get("text")
    message_id = message.get("message_id")
    if not chat_id or not isinstance(text, str) or not isinstance(message_id, int):
        return
    command, argument = _command(text)
    if command == "link":
        if _link_chat(bot, chat, str(argument)):
            _send(bot["token"], chat_id, "✅ <b>Connected.</b> Send <code>/ai NVDA</code> for a stock report or <code>/ai DAB</code> for your Trading 212 account brief.", reply_to_message_id=message_id)
        else:
            _send(bot["token"], chat_id, "❌ This pairing code is invalid or has expired. Create a new one in the website.", reply_to_message_id=message_id)
        return
    if command == "help":
        _send(bot["token"], chat_id, "Use <code>/link &lt;code&gt;</code> once to authorize this chat, then <code>/ai NVDA</code> for a stock report or <code>/ai DAB</code> for a Trading 212 account brief.", reply_to_message_id=message_id)
        return
    if command not in {"report", "daily_account_brief"}:
        return
    with jobs._connection() as conn:
        authorized = _chat_authorized(conn, int(bot["user_id"]), chat_id)
        if authorized:
            conn.execute("UPDATE telegram_ai_bot_chats SET last_seen_at=? WHERE user_id=? AND chat_id=?", (_iso(), int(bot["user_id"]), chat_id))
    if not authorized:
        _send(bot["token"], chat_id, "🔒 This chat is not authorized. Create a pairing code on the website, then send <code>/link &lt;code&gt;</code> here.", reply_to_message_id=message_id)
        return
    try:
        job_id = _queue_daily_account_brief(bot, chat_id, message_id) if command == "daily_account_brief" else _queue_report(bot, chat_id, message_id, str(argument))
    except Exception as exc:
        _send(bot["token"], chat_id, f"❌ Could not queue the report: {str(exc)[:360]}", reply_to_message_id=message_id)
        return
    if command == "daily_account_brief":
        _send(bot["token"], chat_id, f"⏳ <b>Trading 212 Daily Account Brief</b> queued (<code>{job_id[:8]}</code>). I will reply here when it is ready.", reply_to_message_id=message_id)
    else:
        _send(bot["token"], chat_id, f"⏳ <b>{argument}</b> report queued (<code>{job_id[:8]}</code>). I will reply here when research is complete.", reply_to_message_id=message_id)


def poll_one_bot(bot: dict[str, Any]) -> dict[str, int]:
    timeout = max(5, min(int(os.environ.get("TELEGRAM_AI_REPORT_POLL_TIMEOUT_SECONDS", "25")), 45))
    user_id = int(bot["user_id"])
    offset = max(0, int(bot.get("polling_offset") or 0))
    try:
        result = _telegram_api(bot["token"], "getUpdates", payload={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]}, timeout=timeout + 12)
        updates = result.get("result") if isinstance(result.get("result"), list) else []
        next_offset = offset
        processed = 0
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            is_new = not isinstance(update_id, int) or _claim_update(user_id, update_id)
            try:
                if is_new:
                    _process_update(bot, update)
            except Exception as exc:
                # Consume malformed/unusable messages as well; otherwise one bad
                # inbound update would permanently block the polling cursor.
                _record_poll_error(user_id, f"Command handling failed: {type(exc).__name__}: {exc}")
            if isinstance(update_id, int):
                next_offset = max(next_offset, update_id + 1)
            processed += 1
        with jobs._connection() as conn:
            conn.execute("UPDATE telegram_ai_bot_settings SET polling_offset=?, last_polled_at=?, last_error=NULL, updated_at=? WHERE user_id=?", (next_offset, _iso(), _iso(), user_id))
        return {"processed": processed, "failed": 0}
    except Exception as exc:
        _record_poll_error(user_id, str(exc))
        return {"processed": 0, "failed": 1}


def poll_configured_bots() -> dict[str, int]:
    bots = _enabled_bots()
    if not bots:
        return {"bots": 0, "processed": 0, "failed": 0}
    workers = min(len(bots), max(1, int(os.environ.get("TELEGRAM_AI_REPORT_MAX_POLLERS", "8"))))
    processed = failed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="telegram-poll") as executor:
        futures = [executor.submit(poll_one_bot, bot) for bot in bots]
        for future in as_completed(futures):
            result = future.result()
            processed += result.get("processed", 0)
            failed += result.get("failed", 0)
    return {"bots": len(bots), "processed": processed, "failed": failed}


def deliver_telegram_job(job: dict[str, Any], messages: list[str]) -> None:
    try:
        payload = json.loads(job.get("payload_json") or "{}")
    except (TypeError, ValueError) as exc:
        raise TelegramAIReportError("Telegram job payload is invalid.") from exc
    user_id = int(payload.get("telegram_user_id"))
    chat_id = str(payload.get("telegram_chat_id") or "")
    if not chat_id:
        raise TelegramAIReportError("Telegram delivery chat is missing.")
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        settings = _row_dict(conn.execute("SELECT token_encrypted FROM telegram_ai_bot_settings WHERE user_id=? AND enabled=1", (user_id,)).fetchone())
        allowed = _chat_authorized(conn, user_id, chat_id)
    if not settings or not allowed:
        raise TelegramAIReportError("Telegram bot or destination chat is no longer authorized.")
    token = _decrypt(str(settings["token_encrypted"]))
    reply_to = payload.get("telegram_reply_to_message_id")
    for index, message in enumerate(messages):
        _send(token, chat_id, str(message), reply_to_message_id=int(reply_to) if index == 0 and reply_to else None)
    with jobs._connection() as conn:
        conn.execute("UPDATE telegram_ai_report_requests SET status='sent', error=NULL, updated_at=? WHERE job_id=?", (_iso(), str(job["id"])))


def mark_telegram_job_failure(job: dict[str, Any], error: str) -> None:
    init_telegram_ai_report_db()
    with jobs._connection() as conn:
        conn.execute("UPDATE telegram_ai_report_requests SET status='failed', error=?, updated_at=? WHERE job_id=?", (str(error)[:500], _iso(), str(job["id"])))
