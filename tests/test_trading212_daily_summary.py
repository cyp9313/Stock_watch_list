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
        if url.endswith("/equity/account/summary"):
            return _Response({"currency": "EUR", "totalValue": 1200.0, "cash": {"availableToTrade": 300.0}})
        assert url.endswith("/equity/history/orders")
        return _Response({"items": [], "nextPagePath": None})

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


def test_daily_summary_prefers_direct_executed_order_history(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    _save_verified_mapping(path)
    sent = []

    def fake_get(url, **_kwargs):
        if url.endswith("/equity/positions"):
            return _Response(_position())
        if url.endswith("/equity/account/summary"):
            return _Response({"currency": "EUR", "totalValue": 1_000.0, "cash": {"availableToTrade": 100.0}})
        assert url.endswith("/equity/history/orders")
        return _Response({
            "items": [{
                "order": {"id": 11, "ticker": "AAPL_US_EQ", "side": "BUY", "filledQuantity": 3},
                "fill": {"id": 12, "type": "TRADE", "quantity": 3, "price": 101.5, "filledAt": "2026-11-02T20:00:00Z"},
            }],
            "nextPagePath": None,
        })

    result = summary.generate_and_send_daily_summary(
        7, path=path, now=dt.datetime(2026, 11, 2, 21, 15, tzinfo=dt.timezone.utc),
        request_get=fake_get,
        request_post=lambda *_args, **kwargs: (sent.append(kwargs["json"]) or _Response({})),
        ai_runner=lambda _snapshot: {"ok": False, "error": "disabled"},
    )
    assert result["snapshot"]["position_changes_source"] == "orders_history"
    assert result["snapshot"]["position_changes"][0]["kind"] == "bought"
    assert result["snapshot"]["position_changes"][0]["ticker"] == "AAPL"
    assert "Today’s executed trades" in sent[0]["text"]
    assert "<b>AAPL</b> — Bought: 3 shares @ 101.5" in sent[0]["text"]


def test_daily_snapshot_reports_adds_reductions_new_and_closed_positions(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    _save_verified_mapping(path)
    conn = monitor._connect(path)
    try:
        # 2026-11-02 21:15 UTC is 22:15 Berlin time.  These are the final
        # snapshots before that Berlin report day started.
        captured_at = "2026-11-01T22:59:00+00:00"
        for key, ticker, quantity in (
            ("AAPL_US_EQ", "AAPL_US_EQ", 2.0),
            ("MSFT_US_EQ", "MSFT_US_EQ", 4.0),
            ("TSLA_US_EQ", "TSLA_US_EQ", 5.0),
        ):
            conn.execute(
                """INSERT INTO position_snapshots(
                    user_id, captured_at, position_key, ticker, display_name, currency,
                    current_price, quantity, current_value, monitor_clock
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (7, captured_at, key, ticker, ticker, "USD", 100.0, quantity, quantity * 100.0, 1),
            )
        increased = monitor._position_from_api({
            "currentPrice": 101.0, "quantity": 3.0,
            "instrument": {"ticker": "AAPL_US_EQ", "name": "Old Apple name", "currency": "USD"},
            "walletImpact": {"currentValue": 303.0},
        })
        opened = monitor._position_from_api({
            "currentPrice": 200.0, "quantity": 1.5,
            "instrument": {"ticker": "NVDA_US_EQ", "name": "NVIDIA", "currency": "USD"},
            "walletImpact": {"currentValue": 300.0},
        })
        reduced = monitor._position_from_api({
            "currentPrice": 250.0, "quantity": 2.0,
            "instrument": {"ticker": "TSLA_US_EQ", "name": "Tesla", "currency": "USD"},
            "walletImpact": {"currentValue": 500.0},
        })
        assert increased is not None and opened is not None and reduced is not None
        snapshot = summary.build_daily_account_snapshot(
            conn, 7, [increased, opened, reduced], {}, dt.datetime(2026, 11, 2, 21, 15, tzinfo=dt.timezone.utc),
        )
    finally:
        conn.close()

    changes = {item["ticker"]: item for item in snapshot["position_changes"]}
    assert changes["AAPL"]["kind"] == "increased"
    assert changes["AAPL"]["baseline_quantity"] == 2.0
    assert changes["AAPL"]["quantity"] == 3.0
    assert changes["NVDA_US_EQ"]["kind"] == "opened"
    assert changes["TSLA_US_EQ"]["kind"] == "reduced"
    assert changes["MSFT_US_EQ"]["kind"] == "closed"
    text = summary.render_daily_summary_telegram(snapshot, {"ok": False})
    assert "Today’s position changes" in text
    assert "<b>AAPL</b> — Added: 2 → 3 (+1; +50.00%)" in text
    assert "<b>TSLA_US_EQ</b> — Reduced: 5 → 2 (-3; -60.00%)" in text
    assert "<b>MSFT_US_EQ</b> — Closed: 4 → 0 (-4; -100.00%)" in text


def test_first_daily_snapshot_does_not_mislabel_existing_holdings_as_new(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    position = monitor._position_from_api(_position()[0])
    assert position is not None
    conn = monitor._connect(path)
    try:
        snapshot = summary.build_daily_account_snapshot(
            conn, 7, [position], {}, dt.datetime(2026, 11, 2, 21, 15, tzinfo=dt.timezone.utc),
        )
    finally:
        conn.close()
    assert snapshot["position_changes"] == []


def test_daily_snapshot_uses_monitor_account_opening_baseline_for_total_change(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    now = dt.datetime(2026, 11, 2, 21, 15, tzinfo=dt.timezone.utc)
    conn = monitor._connect(path)
    try:
        conn.execute(
            """INSERT INTO account_daily_baselines(
                user_id, trading_date, captured_at, currency, total_value, cash_value
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (7, "2026-11-02", "2026-11-01T23:01:00+00:00", "EUR", 1_000.0, 100.0),
        )
        position = monitor._position_from_api(_position(price=110.0, value=220.0)[0])
        assert position is not None
        snapshot = summary.build_daily_account_snapshot(
            conn, 7, [position], {"currency": "EUR", "totalValue": 1_050.0}, now,
        )
    finally:
        conn.close()

    account = snapshot["account"]
    assert account["daily_change_source"] == "account_summary"
    assert account["daily_change"] == 50.0
    assert account["daily_change_pct"] == 5.0
    text = summary.render_daily_summary_telegram(snapshot, {"ok": False})
    assert "<b>Daily account change</b>: <code>50.00 EUR</code> (+5.00%)" in text


def test_daily_summary_renders_readable_technical_signal_directions(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    now = dt.datetime(2026, 11, 2, 21, 15, tzinfo=dt.timezone.utc)
    conn = monitor._connect(path)
    try:
        for key, side in (("ema_250", "above"), ("rsi_30", "below"), ("macd_diff", "negative")):
            conn.execute(
                """INSERT INTO technical_signal_state(
                    user_id, position_key, signal_key, last_value, last_side, last_observed_at, last_triggered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (7, "AAPL_US_EQ", key, 1.0, side, "2026-11-02T20:00:00+00:00", "2026-11-02T20:00:00+00:00"),
            )
        position = monitor._position_from_api(_position()[0])
        assert position is not None
        snapshot = summary.build_daily_account_snapshot(conn, 7, [position], {}, now)
    finally:
        conn.close()

    labels = snapshot["holdings"][0]["signals"]
    assert labels == [
        "Price crossed above EMA250",
        "Daily MACD death cross (Diff crossed below 0)",
        "RSI(14) crossed below 30",
    ]
    text = summary.render_daily_summary_telegram(snapshot, {"ok": False})
    assert "<b>AAPL_US_EQ</b>: Price crossed above EMA250, Daily MACD death cross" in text


def test_due_summaries_skip_weekends_and_before_the_scheduled_time(tmp_path, monkeypatch):
    path = tmp_path / "monitor.db"
    _configure(monkeypatch, path)
    before = dt.datetime(2026, 11, 2, 20, 0, tzinfo=dt.timezone.utc)  # 21:00 Berlin
    saturday = dt.datetime(2026, 11, 7, 12, 0, tzinfo=dt.timezone.utc)
    assert summary.run_due_daily_summaries(path=path, now=before) == []
    assert summary.run_due_daily_summaries(path=path, now=saturday) == []
