import datetime as dt

from cryptography.fernet import Fernet

import trading212_daily_summary as summary
import trading212_monitor as monitor


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _position(price=101.0, value=202.0):
    return [{
        "currentPrice": price,
        "quantity": 2,
        "instrument": {"ticker": "AAPL_US_EQ", "name": "Apple Inc.", "currency": "USD"},
        "walletImpact": {"currentValue": value, "currency": "EUR"},
    }]


def _configure(monkeypatch, path):
    monkeypatch.setenv("TRADING212_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monitor.save_monitor_settings(
        7, enabled=True, api_key="key", api_secret="secret", telegram_bot_token="token", telegram_chat_id="chat", path=path,
    )
    summary.save_daily_summary_settings(
        7, enabled=True, hour=22, minute=15, include_ai=True, include_news=True, path=path,
    )


def _save_verified_mapping(path, *, broker_ticker="AAPL_US_EQ", yfinance_ticker="AAPL"):
    position = monitor._position_from_api(_position()[0])
    assert position is not None
    position["ticker"] = broker_ticker
    position["position_key"] = broker_ticker
    conn = monitor._connect(path)
    try:
        monitor._upsert_mapping(
            conn, position, yfinance_ticker=yfinance_ticker, mapping_status="mapped",
            mapping_source="test", last_history_check_date="2026-11-02",
            last_volume_history_check_date="2026-11-02", last_history_date="2026-10-30", last_error=None,
        )
    finally:
        conn.close()


def test_daily_summary_sends_one_safe_ai_brief_and_persists_history(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    _save_verified_mapping(path)
    sent = []

    def fake_get(url, **_kwargs):
        if url.endswith("/equity/positions"):
            return _Response(_position())
        assert url.endswith("/equity/account/summary")
        return _Response({"currency": "EUR", "totalValue": 1200.0, "cash": {"availableToTrade": 300.0}})

    def fake_post(*_args, **kwargs):
        sent.append(kwargs["json"])
        return _Response({})

    def fake_ai(snapshot):
        assert snapshot["focus_holdings"][0]["ticker"] == "AAPL"
        assert snapshot["focus_holdings"][0]["broker_ticker"] == "AAPL_US_EQ"
        return {
            "ok": True, "headline": "组合波动上升，重点关注苹果仓位。", "summary": ["持仓集中度需要关注。"],
            "news": [{"ticker": "AAPL", "headline": "Apple update", "url": "https://example.com/news"}],
            "risks": ["单一持仓波动风险。"], "next_focus": ["观察技术信号。"],
        }

    now = dt.datetime(2026, 11, 2, 21, 15, tzinfo=dt.timezone.utc)
    result = summary.generate_and_send_daily_summary(
        7, path=path, now=now, request_get=fake_get, request_post=fake_post, ai_runner=fake_ai,
    )
    assert result["status"] == "sent"
    assert sent[0]["parse_mode"] == "HTML"
    assert "<b>TRADING 212 DAILY ACCOUNT BRIEF</b>" in sent[0]["text"]
    assert "<b>AAPL</b>" in sent[0]["text"]
    assert "AAPL_US_EQ" not in sent[0]["text"]
    assert "<a href='https://example.com/news'>Apple update</a>" in sent[0]["text"]
    assert summary.generate_and_send_daily_summary(7, path=path, now=now, request_get=fake_get, request_post=fake_post)["status"] == "already_processed"
    history = summary.get_daily_summary_history(7, path=path)
    assert history[0]["status"] == "sent"
    assert history[0]["headline"] == "组合波动上升，重点关注苹果仓位。"
    # Scheduler skips a completed date instead of repeatedly calling the API
    # and model on every subsequent minute.
    assert summary.run_due_daily_summaries(path=path, now=now) == []


def test_daily_summary_filters_unknown_tickers_and_unsafe_urls():
    snapshot = {"focus_holdings": [{"ticker": "AAPL_US_EQ"}]}
    ai = summary._normalise_ai_payload({
        "headline": "ok", "summary": ["one"],
        "news": [
            {"ticker": "UNKNOWN", "source_url": "https://example.com", "headline": "ignore"},
            {"ticker": "AAPL_US_EQ", "source_url": "http://127.0.0.1/internal", "headline": "ignore"},
            {"ticker": "AAPL_US_EQ", "source_url": "https://example.com/ok", "headline": "keep"},
        ],
    }, snapshot, [{"url": "https://example.com/ok", "title": "source", "date": "2026-11-02"}])
    assert ai["news"] == [{
        "ticker": "AAPL_US_EQ", "headline": "keep", "why_it_matters": "", "url": "https://example.com/ok",
        "published_date": "", "source_returned": True,
    }]


def test_daily_summary_falls_back_to_positions_when_account_summary_permission_is_missing(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    sent = []

    def fake_get(url, **_kwargs):
        if url.endswith("/equity/positions"):
            return _Response(_position())
        return _Response({}, status_code=403)

    result = summary.generate_and_send_daily_summary(
        7, path=path, now=dt.datetime(2026, 11, 2, 21, 15, tzinfo=dt.timezone.utc),
        request_get=fake_get,
        request_post=lambda *_args, **kwargs: (sent.append(kwargs["json"]) or _Response({})),
        ai_runner=lambda _snapshot: {"ok": False, "error": "disabled"},
    )
    assert result["status"] == "sent"
    assert result["snapshot"]["account"]["total_value"] is None
    assert any("account-summary" in item for item in result["snapshot"]["data_limitations"])
    assert "AI/news view unavailable" in sent[0]["text"]


def test_due_summaries_skip_weekends_and_before_the_scheduled_time(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    before = dt.datetime(2026, 11, 2, 20, 0, tzinfo=dt.timezone.utc)  # 21:00 Berlin
    saturday = dt.datetime(2026, 11, 7, 12, 0, tzinfo=dt.timezone.utc)
    assert summary.run_due_daily_summaries(path=path, now=before) == []
    assert summary.run_due_daily_summaries(path=path, now=saturday) == []
