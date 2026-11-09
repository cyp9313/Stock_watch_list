"""Regression coverage for encrypted Trading 212 monitor state and alerting."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from cryptography.fernet import Fernet

import trading212_monitor as monitor


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _position(price):
    return [{
        "currentPrice": price,
        "quantity": 2,
        "instrument": {"ticker": "AAPL_US_EQ", "name": "Apple Inc.", "currency": "USD"},
        "walletImpact": {"currentValue": price * 2, "currency": "EUR"},
    }]


def _configure(monkeypatch, path: Path, *, enabled=True, policy=None):
    monkeypatch.setenv("TRADING212_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    return monitor.save_monitor_settings(
        7, enabled=enabled, api_key="api-key", api_secret="api-secret",
        telegram_bot_token="telegram-token", telegram_chat_id="12345",
        alert_policy=policy or {
            "windows": {"1": True}, "thresholds": {"1": [1.0]},
            # Existing price-alert tests must not make a live yfinance call.
            "technical": {"ema_crosses": False, "macd_crosses": False, "rsi_levels": False, "volume_surge": False},
        }, path=path,
    )


def _completed_daily_history(_ticker, _end_date):
    """Two years is sufficient; 300 synthetic completed bars exercise EMA250."""
    start = dt.date(2025, 1, 1)
    return [((start + dt.timedelta(days=index)).isoformat(), 100.0, 1_000.0) for index in range(300)]


def test_credentials_are_encrypted_and_status_is_masked(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    status = monitor.get_monitor_status(7, path=path)
    assert status["has_trading212_credentials"] is True
    assert status["has_telegram"] is True
    assert "api-key" not in repr(status)
    conn = monitor._connect(path)
    try:
        row = conn.execute("SELECT api_key_enc, telegram_bot_token_enc FROM monitor_accounts WHERE user_id=7").fetchone()
    finally:
        conn.close()
    assert row["api_key_enc"] != "api-key"
    assert row["telegram_bot_token_enc"] != "telegram-token"


def test_alert_crossing_rearms_and_uses_rolling_snapshot_retention(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    sent = []
    prices = iter((100.0, 101.1, 101.2, 102.4))

    def fake_get(*_args, **_kwargs):
        return _Response(_position(next(prices)))

    def fake_post(*_args, **kwargs):
        sent.append(kwargs["json"]["text"])
        return _Response({"ok": True})

    base = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)  # Monday
    assert monitor.run_account_monitor(7, path=path, now=base, request_get=fake_get, request_post=fake_post)["events"] == []
    second = monitor.run_account_monitor(7, path=path, now=base + dt.timedelta(minutes=1), request_get=fake_get, request_post=fake_post)
    assert len(second["events"]) == 1
    assert "+1.10%" in sent[-1]
    # Still beyond the same tier: no Telegram spam.
    third = monitor.run_account_monitor(7, path=path, now=base + dt.timedelta(minutes=2), request_get=fake_get, request_post=fake_post)
    assert third["events"] == []
    # The small move at minute 2 rearms; minute 3 crosses again.
    fourth = monitor.run_account_monitor(7, path=path, now=base + dt.timedelta(minutes=3), request_get=fake_get, request_post=fake_post)
    assert len(fourth["events"]) == 1
    assert len(sent) == 2

    conn = monitor._connect(path)
    try:
        conn.execute(
            "INSERT INTO position_snapshots(user_id, captured_at, position_key, ticker, current_price, monitor_clock) VALUES (?, ?, ?, ?, ?, ?)",
            (7, "2026-10-30T12:00:00+00:00", "OLD_US_EQ", "OLD_US_EQ", 1.0, monitor._monitor_clock(dt.datetime(2026, 10, 30, 12, tzinfo=dt.timezone.utc))),
        )
    finally:
        conn.close()
    # One more successful minute prunes only data older than the moving 48h boundary.
    monitor.run_account_monitor(7, path=path, now=base + dt.timedelta(hours=49), request_get=lambda *_a, **_k: _Response(_position(102.4)), request_post=fake_post)
    conn = monitor._connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM position_snapshots WHERE position_key='OLD_US_EQ'").fetchone()[0] == 0
    finally:
        conn.close()


def test_telegram_alert_html_highlights_trigger_and_escapes_ticker():
    message = monitor._telegram_message([{
        "ticker": "AAPL<US>", "current_price": 101.25, "currency": "USD",
        "window_minutes": 5, "change_pct": 2.25, "threshold_pct": 2.0, "direction": "up",
    }], dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc))

    assert "🚨 <b>TRADING 212 POSITION ALERT</b>" in message
    assert "🟢📈 <b>AAPL&lt;US&gt;</b>" in message
    assert "<b>5m MOVE +2.25%</b>" in message
    assert "Trigger: <b>±2%</b>" in message
    assert "<script>" not in message


def test_telegram_ema_alert_identifies_the_exact_period_and_prefers_mapped_ticker():
    message = monitor._telegram_message([{
        "ticker": "FB_US_EQ", "report_ticker": "META", "current_price": 612.5, "currency": "USD",
        "event_type": "technical", "technical_kind": "ema_cross", "ema_period": 50,
        "direction": "up", "detail": "Price crossed above EMA 50",
    }], dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc))

    assert "<b>META</b>" in message
    assert "FB_US_EQ" not in message
    assert "<b>PRICE CROSSED ABOVE EMA 50</b>" in message


def test_telegram_html_parse_failure_retries_as_plain_text():
    calls = []

    def fake_post(*_args, **kwargs):
        calls.append(kwargs["json"])
        return _Response({}, status_code=400 if len(calls) == 1 else 200)

    monitor.send_telegram_message("token", "chat", "<b>Alert</b>", request_post=fake_post)
    assert calls[0]["parse_mode"] == "HTML"
    assert calls[1]["text"] == "Alert"
    assert "parse_mode" not in calls[1]


def test_telegram_test_uses_the_same_rich_alert_format(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    sent = []

    def fake_post(*_args, **kwargs):
        sent.append(kwargs["json"])
        return _Response({})

    monitor.send_telegram_test(7, path=path, request_post=fake_post)
    assert sent[0]["parse_mode"] == "HTML"
    assert "<b>SAMPLE</b>" in sent[0]["text"]
    assert "<b>5m MOVE +2.25%</b>" in sent[0]["text"]


def test_weekend_pauses_without_calling_trading212(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    called = False

    def fake_get(*_args, **_kwargs):
        nonlocal called
        called = True
        return _Response(_position(100.0))

    # Saturday morning in Berlin.
    saturday = dt.datetime(2026, 11, 7, 11, 0, tzinfo=dt.timezone.utc)
    result = monitor.run_account_monitor(7, path=path, now=saturday, request_get=fake_get)
    assert result["status"] == "weekend_paused"
    assert called is False


def test_retention_prunes_while_market_monitoring_is_weekend_paused(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    conn = monitor._connect(path)
    try:
        conn.execute(
            "INSERT INTO position_snapshots(user_id, captured_at, position_key, ticker, current_price, monitor_clock) VALUES (?, ?, ?, ?, ?, ?)",
            (7, "2026-11-04T10:00:00+00:00", "OLD_US_EQ", "OLD_US_EQ", 1.0, monitor._monitor_clock(dt.datetime(2026, 11, 4, 10, tzinfo=dt.timezone.utc))),
        )
    finally:
        conn.close()
    saturday = dt.datetime(2026, 11, 7, 11, 0, tzinfo=dt.timezone.utc)
    assert monitor.prune_expired_snapshots(path=path, now=saturday) == 1


def test_weekend_freeze_keeps_friday_state_as_monday_baseline(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    sent = []
    prices = iter((100.0, 101.5))
    friday_final = dt.datetime(2026, 11, 7, 0, 59, 30, tzinfo=dt.timezone.utc)  # Saturday 01:59:30 Berlin
    monday_resume = dt.datetime(2026, 11, 9, 1, 0, tzinfo=dt.timezone.utc)  # Monday 02:00 Berlin

    def fake_get(*_args, **_kwargs):
        return _Response(_position(next(prices)))

    def fake_post(*_args, **kwargs):
        sent.append(kwargs["json"]["text"])
        return _Response({"ok": True})

    monitor.run_account_monitor(7, path=path, now=friday_final, request_get=fake_get, request_post=fake_post)
    # Weekend cleanup does not age Friday's snapshot by 48 wall-clock hours.
    assert monitor.prune_expired_snapshots(path=path, now=monday_resume) == 0
    resumed = monitor.run_account_monitor(7, path=path, now=monday_resume, request_get=fake_get, request_post=fake_post)
    assert len(resumed["events"]) == 1
    assert len(sent) == 1
    assert "resumed after pause" in sent[0]


def test_policy_rejects_unapproved_tiers_and_requires_secrets_to_enable(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    monkeypatch.setenv("TRADING212_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    normalized = monitor.normalize_alert_policy({"windows": {"1": True}, "thresholds": {"1": [1, 3, 10]}})
    assert normalized["thresholds"]["1"] == [1.0, 10.0]
    try:
        monitor.save_monitor_settings(7, enabled=True, path=path)
    except ValueError as exc:
        assert "API Key" in str(exc)
    else:
        raise AssertionError("Enabling without broker credentials must fail")


def test_trading212_tickers_map_to_conservative_yfinance_candidates():
    assert monitor._t212_yfinance_candidates({"ticker": "AAPL_US_EQ", "exchange_code": ""})[0] == "AAPL"
    assert monitor._t212_yfinance_candidates({"ticker": "VUSA_L_EQ", "exchange_code": ""})[0] == "VUSA.L"
    assert monitor._t212_yfinance_candidates({"ticker": "BAD_UNKNOWN_EQ", "exchange_code": ""}) == ["BAD_UNKNOWN_EQ"]


def test_technical_alerts_use_cached_completed_history_and_live_price(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    policy = {
        "windows": {str(minutes): False for minutes in monitor.WINDOWS_MINUTES},
        "thresholds": {},
        "technical": {"ema_crosses": True, "macd_crosses": True, "rsi_levels": True},
    }
    _configure(monkeypatch, path, policy=policy)
    history_calls = []
    sent = []
    prices = iter((50.0, 200.0))

    def fake_history(ticker, end_date):
        history_calls.append((ticker, end_date))
        return _completed_daily_history(ticker, end_date)

    def fake_get(*_args, **_kwargs):
        return _Response(_position(next(prices)))

    def fake_post(*_args, **kwargs):
        sent.append(kwargs["json"]["text"])
        return _Response({"ok": True})

    base = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)
    first = monitor.run_account_monitor(
        7, path=path, now=base, request_get=fake_get, request_post=fake_post, history_fetcher=fake_history,
    )
    # Initial values are a baseline, never a retrospective alert.
    assert first["events"] == []
    second = monitor.run_account_monitor(
        7, path=path, now=base + dt.timedelta(minutes=1), request_get=fake_get,
        request_post=fake_post, history_fetcher=fake_history,
    )
    kinds = {event.get("technical_kind") for event in second["events"]}
    assert {"ema_cross", "macd_cross", "rsi_level_cross"} <= kinds
    assert "EMA 20" in sent[-1]
    assert "RSI(14)" in sent[-1]
    # Both minute runs reuse the same completed-bar cache; only the live price changes.
    assert history_calls == [("AAPL", dt.date(2026, 11, 2))]
    conn = monitor._connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM daily_price_history WHERE yfinance_ticker='AAPL'").fetchone()[0] == 300
        assert conn.execute(
            "SELECT COUNT(*) FROM technical_signal_state WHERE user_id=7 AND position_key='AAPL_US_EQ'"
        ).fetchone()[0] >= 7
    finally:
        conn.close()


def test_unmapped_technical_history_does_not_block_price_monitoring(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)

    def fake_get(*_args, **_kwargs):
        return _Response(_position(100.0))

    # Enable technical signals separately from the price-move test policy.
    monitor.save_monitor_settings(
        7, enabled=True,
        alert_policy={
            "windows": {"1": True}, "thresholds": {"1": [1.0]},
            "technical": {"ema_crosses": True, "macd_crosses": True, "rsi_levels": True},
        }, path=path,
    )
    result = monitor.run_account_monitor(
        7, path=path, now=dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone.utc),
        request_get=fake_get, history_fetcher=lambda *_args: [],
    )
    assert result["status"] == "ok"
    assert result["technical_unavailable"] == {"AAPL_US_EQ": "daily_history_empty"}


def test_trading212_5x24_window_uses_berlin_two_am_boundaries():
    assert monitor.is_monitoring_window(dt.datetime(2026, 11, 9, 0, 59, tzinfo=dt.timezone.utc)) is False
    assert monitor.is_monitoring_window(dt.datetime(2026, 11, 9, 1, 0, tzinfo=dt.timezone.utc)) is True
    assert monitor.is_monitoring_window(dt.datetime(2026, 11, 7, 0, 59, tzinfo=dt.timezone.utc)) is True
    assert monitor.is_monitoring_window(dt.datetime(2026, 11, 7, 1, 0, tzinfo=dt.timezone.utc)) is False


def test_volume_surge_is_cached_for_five_minutes_and_only_alerts_once(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    policy = {
        "windows": {str(minutes): False for minutes in monitor.WINDOWS_MINUTES},
        "thresholds": {},
        "technical": {"ema_crosses": False, "macd_crosses": False, "rsi_levels": False, "volume_surge": True},
        "volume": {"relative_ratio_threshold": 2.0},
    }
    _configure(monkeypatch, path, policy=policy)
    volume_calls = []
    sent = []

    def fake_get(*_args, **_kwargs):
        return _Response(_position(100.0))

    def fake_volume(ticker):
        volume_calls.append(ticker)
        return 200.0

    def fake_post(*_args, **kwargs):
        sent.append(kwargs["json"]["text"])
        return _Response({"ok": True})

    # 10:00 New York: 30 minutes into the US regular session.  With a 1,000
    # 20-day average, expected cumulative volume is about 77; 200 is a surge.
    base = dt.datetime(2026, 11, 2, 15, 0, tzinfo=dt.timezone.utc)
    first = monitor.run_account_monitor(
        7, path=path, now=base, request_get=fake_get, request_post=fake_post,
        history_fetcher=_completed_daily_history, volume_fetcher=fake_volume,
    )
    assert [event["technical_kind"] for event in first["events"]] == ["volume_surge"]
    second = monitor.run_account_monitor(
        7, path=path, now=base + dt.timedelta(minutes=2), request_get=fake_get, request_post=fake_post,
        history_fetcher=_completed_daily_history, volume_fetcher=fake_volume,
    )
    assert second["events"] == []
    assert volume_calls == ["AAPL"]
    assert "Regular-session volume" in sent[0]


def test_user_can_review_and_override_a_mapping_after_a_position_is_observed(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path, policy={
        "windows": {str(minutes): False for minutes in monitor.WINDOWS_MINUTES},
        "thresholds": {},
        "technical": {"ema_crosses": True, "macd_crosses": False, "rsi_levels": False, "volume_surge": False},
    })
    monitor.run_account_monitor(
        7, path=path, now=dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone.utc),
        request_get=lambda *_args, **_kwargs: _Response(_position(100.0)),
        history_fetcher=_completed_daily_history,
    )
    mappings = monitor.list_position_mappings(7, path=path)
    assert mappings[0]["yfinance_ticker"] == "AAPL"
    monitor.save_yfinance_mapping_override(7, "AAPL_US_EQ", "AAPL", path=path)
    assert monitor.list_position_mappings(7, path=path)[0]["mapping_source"] == "manual"


def test_mapping_refresh_works_outside_monitor_window_without_snapshots(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path, enabled=False)
    # Saturday noon Berlin is outside the 5x24 alert/snapshot window.
    saturday = dt.datetime(2026, 11, 7, 11, 0, tzinfo=dt.timezone.utc)
    refreshed = monitor.refresh_position_mappings(
        7, path=path, now=saturday,
        request_get=lambda *_args, **_kwargs: _Response(_position(100.0)),
        history_fetcher=_completed_daily_history,
    )
    assert refreshed == {"positions": 1, "mapped": 1, "unavailable": {}}
    assert monitor.list_position_mappings(7, path=path)[0]["yfinance_ticker"] == "AAPL"
    conn = monitor._connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM position_snapshots WHERE user_id=7").fetchone()[0] == 0
    finally:
        conn.close()
