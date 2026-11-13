from __future__ import annotations

import importlib
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


def _load_modules(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REPORT_JOB_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("TELEGRAM_AI_REPORT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import daily_report.jobs as job_module
    job_module._INITIALIZED_DATABASES.clear()
    import telegram_ai_reports as telegram_module
    telegram_module = importlib.reload(telegram_module)
    monkeypatch.setattr(telegram_module, "_discard_pending_updates", lambda _token: (0, 0))
    return importlib.reload(job_module), telegram_module


def test_command_parser_accepts_private_and_group_forms(tmp_path: Path, monkeypatch) -> None:
    _, telegram = _load_modules(tmp_path, monkeypatch)
    assert telegram._command("/ai nvda") == ("report", "NVDA")
    assert telegram._command("/ai@MyBot BRK-B") == ("report", "BRK-B")
    assert telegram._command("@AI BTC-USD") == ("report", "BTC-USD")
    assert telegram._command("/ai DAB") == ("daily_account_brief", None)
    assert telegram._command("/link token_12345678") == ("link", "token_12345678")
    assert telegram._command("hello") == ("", None)


def test_pairing_code_authorizes_chat_and_queues_only_paired_command(tmp_path: Path, monkeypatch) -> None:
    jobs, telegram = _load_modules(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram, "_validate_token", lambda token: ("123", "example_bot"))
    sent: list[str] = []
    monkeypatch.setattr(telegram, "_send", lambda _token, _chat, text, **_kwargs: sent.append(text))
    telegram.save_telegram_ai_bot_settings(user_id=7, owner_key="scope-7", bot_token="123:test", enabled=True)
    bot_id = telegram.get_telegram_ai_bot_status(7)["bots"][0]["id"]
    pairing = telegram.create_link_code(7, bot_id)
    bot = telegram._enabled_bots()[0]
    telegram._process_update(bot, {
        "message": {"message_id": 3, "text": f"/link {pairing['code']}", "chat": {"id": 88, "type": "private", "username": "tester"}},
    })
    telegram._process_update(bot, {
        "message": {"message_id": 4, "text": "/ai NVDA", "chat": {"id": 88, "type": "private", "username": "tester"}},
    })
    status = telegram.get_telegram_ai_bot_status(7)
    assert len(status["chats"]) == 1
    assert status["recent_requests"][0]["ticker"] == "NVDA"
    assert any("queued" in text for text in sent)
    queued = [job for job in jobs.list_owner_jobs("scope-7") if job.get("report_kind") == "telegram_ticker"]
    assert queued[0]["subject_key"] == "NVDA"


def test_paired_chat_queues_dab_without_using_ticker_report_quota(tmp_path: Path, monkeypatch) -> None:
    jobs, telegram = _load_modules(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram, "_validate_token", lambda token: ("123", "example_bot"))
    sent: list[str] = []
    monkeypatch.setattr(telegram, "_send", lambda _token, _chat, text, **_kwargs: sent.append(text))
    telegram.save_telegram_ai_bot_settings(user_id=7, owner_key="scope-7", bot_token="123:test", enabled=True)
    bot_id = telegram.get_telegram_ai_bot_status(7)["bots"][0]["id"]
    pairing = telegram.create_link_code(7, bot_id)
    bot = telegram._enabled_bots()[0]
    telegram._process_update(bot, {
        "message": {"message_id": 3, "text": f"/link {pairing['code']}", "chat": {"id": 88, "type": "private"}},
    })
    telegram._process_update(bot, {
        "message": {"message_id": 4, "text": "/ai DAB", "chat": {"id": 88, "type": "private"}},
    })
    queued = [job for job in jobs.list_owner_jobs("scope-7") if job.get("report_kind") == "telegram_daily_account_brief"]
    assert len(queued) == 1
    assert queued[0]["subject_key"] == "DAB"
    assert any("Daily Account Brief" in text and "queued" in text for text in sent)


def test_initial_bot_setup_advances_past_old_updates(tmp_path: Path, monkeypatch) -> None:
    _jobs, telegram = _load_modules(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram, "_validate_token", lambda token: ("123", "example_bot"))
    monkeypatch.setattr(telegram, "_discard_pending_updates", lambda _token: (987, 4))
    status = telegram.save_telegram_ai_bot_settings(
        user_id=7, owner_key="scope-7", bot_token="123:test", enabled=True,
    )
    assert status["discarded_pending_updates"] == 4
    assert telegram._enabled_bots()[0]["polling_offset"] == 987


def test_one_account_can_add_and_pair_multiple_bots(tmp_path: Path, monkeypatch) -> None:
    _jobs, telegram = _load_modules(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram, "_validate_token", lambda token: (token.split(":", 1)[0], f"bot_{token[0]}"))
    telegram.save_telegram_ai_bot_settings(user_id=7, owner_key="scope-7", bot_token="1:one", enabled=True)
    telegram.save_telegram_ai_bot_settings(user_id=7, owner_key="scope-7", bot_token="2:two", enabled=True)
    status = telegram.get_telegram_ai_bot_status(7)
    assert len(status["bots"]) == 2
    assert "token_encrypted" not in status["bots"][0]
    assert len(telegram._enabled_bots()) == 2


def test_legacy_single_bot_configuration_is_migrated_without_losing_chat(tmp_path: Path, monkeypatch) -> None:
    jobs, telegram = _load_modules(tmp_path, monkeypatch)
    telegram.init_telegram_ai_report_db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    token = "123:legacy"
    with jobs._connection() as conn:
        conn.execute(
            """INSERT INTO telegram_ai_bot_settings(
                user_id, owner_key, token_encrypted, token_fingerprint, bot_username, enabled,
                polling_offset, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (7, "scope-7", telegram._encrypt(token), telegram._fingerprint(token), "legacy_bot", 1, 44, now, now),
        )
        conn.execute(
            """INSERT INTO telegram_ai_bot_chats(
                user_id, chat_id, chat_type, chat_title, authorized_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (7, "88", "private", "tester", now, now),
        )
    telegram.init_telegram_ai_report_db()
    status = telegram.get_telegram_ai_bot_status(7)
    assert len(status["bots"]) == 1
    assert status["bots"][0]["bot_username"] == "legacy_bot"
    assert status["bots"][0]["chats"][0]["chat_id"] == "88"


def test_repeated_telegram_update_is_processed_only_once(tmp_path: Path, monkeypatch) -> None:
    _jobs, telegram = _load_modules(tmp_path, monkeypatch)
    seen: list[dict] = []
    monkeypatch.setattr(telegram, "_process_update", lambda _bot, update: seen.append(update))
    monkeypatch.setattr(telegram, "_telegram_api", lambda *_args, **_kwargs: {
        "result": [{"update_id": 712, "message": {"message_id": 1, "text": "/help", "chat": {"id": 88}}}]
    })
    bot = {"id": "bot-test", "user_id": 7, "token": "123:test", "polling_offset": 0}
    telegram.poll_one_bot(bot)
    # Simulate a stale cursor / getUpdates retry returning the same update.
    telegram.poll_one_bot(bot)
    assert len(seen) == 1


def test_telegram_renderer_escapes_provider_text_and_splits_long_content() -> None:
    from daily_report.telegram_stock_report import render_telegram_stock_report

    messages = render_telegram_stock_report(
        ticker="AAPL",
        report_date="2026-11-13",
        data={"CURRENCY": "USD", "LONG_NAME": "Apple <unsafe>", "LAST_CLOSE": 100, "PCT": 1.5, "rsi": 55, "ma20": 99, "ma50": 98, "ma200": 95, "technical_signal": "bullish", "final_rating": {"final_score": 66, "rating_text": "Hold"}},
        decision={"final_score": 66, "final_action": "watch", "one_sentence": "x" * 6000, "catalysts": [], "risk_alerts": [], "action_checklist": []},
        notes={"items": [{"title": "Source <one>", "url": "https://example.com"}]},
    )
    assert messages
    assert all(len(message) <= 3800 + 30 for message in messages)
    assert "Apple &lt;unsafe&gt;" in "\n".join(messages)
