import sqlite3

import multiuser_store


def _screener():
    return {
        "version": "test-v1",
        "strategies": [{
            "key": "trend_quality", "label": "趋势质量", "matched_count": 1,
            "candidates": [{"Ticker": "AAPL", "Score": 90}],
        }],
    }


def test_screening_history_is_account_isolated_and_stores_full_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(multiuser_store, "USER_DB_PATH", str(tmp_path / "users.db"))
    multiuser_store.create_user("first", "safe-password-123")
    multiuser_store.create_user("second", "safe-password-456")
    first = multiuser_store.authenticate("first", "safe-password-123")
    second = multiuser_store.authenticate("second", "safe-password-456")
    run_ids = multiuser_store.save_screening_runs(first["id"], _screener(), {"eligible_equities": 1})

    runs = multiuser_store.list_screening_runs(first["id"])
    assert len(runs) == 1
    assert multiuser_store.list_screening_runs(second["id"]) == []
    stored = multiuser_store.get_screening_run(first["id"], run_ids["trend_quality"])
    assert stored["result"]["candidates"][0]["Ticker"] == "AAPL"
    assert multiuser_store.update_screening_rerank(first["id"], run_ids["trend_quality"], {"ok": True, "ranking": ["AAPL"]})
    assert multiuser_store.get_screening_run(first["id"], run_ids["trend_quality"])["rerank"]["ranking"] == ["AAPL"]


def test_screening_history_removes_entries_older_than_90_days(tmp_path, monkeypatch):
    monkeypatch.setattr(multiuser_store, "USER_DB_PATH", str(tmp_path / "users.db"))
    multiuser_store.create_user("owner", "safe-password-123")
    user = multiuser_store.authenticate("owner", "safe-password-123")
    multiuser_store.save_screening_runs(user["id"], _screener(), {})
    conn = sqlite3.connect(multiuser_store.USER_DB_PATH)
    conn.execute("UPDATE screening_runs SET created_at='2000-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    multiuser_store.save_screening_runs(user["id"], _screener(), {})
    assert len(multiuser_store.list_screening_runs(user["id"])) == 1
