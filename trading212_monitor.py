"""Account-scoped, read-only Trading 212 position monitoring.

This module deliberately owns a separate SQLite database.  The Streamlit
application, market-data cache and report queue must never contend with the
minute-level monitor writer.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = APP_ROOT / "data" / "trading212_monitor.db"
BERLIN_TZ = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc

WINDOWS_MINUTES = (1, 5, 15, 30, 60, 240, 1440)
THRESHOLDS_BY_WINDOW = {
    1: (1.0, 2.0, 5.0, 10.0),
    5: (1.0, 2.0, 5.0, 10.0),
    15: (1.0, 2.0, 5.0, 10.0),
    30: (2.0, 5.0, 10.0),
    60: (2.0, 5.0, 10.0),
    240: (5.0, 10.0),
    1440: (5.0, 10.0),
}
REARM_RATIO = 0.80


class MonitorConfigurationError(ValueError):
    """Raised when encrypted monitoring credentials are unavailable."""


class Trading212APIError(RuntimeError):
    """Safe-to-display error returned by Trading 212 or Telegram."""


def monitor_db_path() -> Path:
    configured = os.environ.get("TRADING212_MONITOR_DB", "").strip()
    return Path(configured) if configured else DEFAULT_DB_PATH


def _now() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or monitor_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_accounts (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            environment TEXT NOT NULL DEFAULT 'live',
            api_key_enc TEXT,
            api_secret_enc TEXT,
            telegram_bot_token_enc TEXT,
            telegram_chat_id_enc TEXT,
            alert_policy_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS position_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            monitor_clock INTEGER,
            position_key TEXT NOT NULL,
            ticker TEXT NOT NULL,
            display_name TEXT,
            currency TEXT,
            current_price REAL NOT NULL,
            quantity REAL,
            current_value REAL,
            UNIQUE(user_id, captured_at, position_key)
        )
        """
    )
    snapshot_columns = {row[1] for row in conn.execute("PRAGMA table_info(position_snapshots)").fetchall()}
    if "monitor_clock" not in snapshot_columns:
        # Existing early-adopter data has no reliable trading-time clock.  It
        # remains visible for retention purposes but is never used as an alert
        # baseline until a fresh snapshot is collected.
        conn.execute("ALTER TABLE position_snapshots ADD COLUMN monitor_clock INTEGER")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_position_snapshots_lookup
        ON position_snapshots(user_id, position_key, captured_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_position_snapshots_monitor_clock
        ON position_snapshots(user_id, position_key, monitor_clock DESC)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_state (
            user_id INTEGER NOT NULL,
            position_key TEXT NOT NULL,
            window_minutes INTEGER NOT NULL,
            threshold_pct REAL NOT NULL,
            direction TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            last_triggered_at TEXT,
            PRIMARY KEY(user_id, position_key, window_minutes, threshold_pct, direction)
        )
        """
    )


def _fernet() -> Fernet:
    value = os.environ.get("TRADING212_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not value:
        raise MonitorConfigurationError(
            "TRADING212_CREDENTIAL_ENCRYPTION_KEY is not configured; monitoring credentials cannot be stored safely."
        )
    try:
        return Fernet(value.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise MonitorConfigurationError("TRADING212_CREDENTIAL_ENCRYPTION_KEY is invalid.") from exc


def _encrypt(value: str) -> str:
    return _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")


def _decrypt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise MonitorConfigurationError("Stored monitor credentials cannot be decrypted.") from exc


def default_alert_policy() -> dict[str, Any]:
    return {
        "windows": {str(minutes): True for minutes in WINDOWS_MINUTES},
        "thresholds": {
            str(minutes): list(values) for minutes, values in THRESHOLDS_BY_WINDOW.items()
        },
    }


def normalize_alert_policy(raw: Any) -> dict[str, Any]:
    defaults = default_alert_policy()
    raw = raw if isinstance(raw, dict) else {}
    raw_windows = raw.get("windows") if isinstance(raw.get("windows"), dict) else {}
    raw_thresholds = raw.get("thresholds") if isinstance(raw.get("thresholds"), dict) else {}
    windows: dict[str, bool] = {}
    thresholds: dict[str, list[float]] = {}
    for minutes in WINDOWS_MINUTES:
        key = str(minutes)
        windows[key] = bool(raw_windows.get(key, defaults["windows"][key]))
        permitted = set(THRESHOLDS_BY_WINDOW[minutes])
        selected = raw_thresholds.get(key, defaults["thresholds"][key])
        if not isinstance(selected, (list, tuple)):
            selected = defaults["thresholds"][key]
        cleaned = []
        for value in selected:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric in permitted and numeric not in cleaned:
                cleaned.append(numeric)
        thresholds[key] = sorted(cleaned)
    return {"windows": windows, "thresholds": thresholds}


def _safe_error(exc: Exception | str) -> str:
    text = str(exc).replace("\n", " ").strip()
    return (text[:240] or type(exc).__name__).replace("Bearer ", "[REDACTED] ")


def get_monitor_status(user_id: int, *, path: Path | None = None) -> dict[str, Any]:
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM monitor_accounts WHERE user_id=?", (int(user_id),)).fetchone()
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM position_snapshots WHERE user_id=?", (int(user_id),)
        ).fetchone()[0]
    finally:
        conn.close()
    if row is None:
        return {
            "configured": False, "enabled": False, "environment": "live",
            "has_trading212_credentials": False, "has_telegram": False,
            "alert_policy": default_alert_policy(), "snapshot_count": 0,
            "last_success_at": None, "last_error": None,
        }
    return {
        "configured": True,
        "enabled": bool(row["enabled"]),
        "environment": row["environment"],
        "has_trading212_credentials": bool(row["api_key_enc"] and row["api_secret_enc"]),
        "has_telegram": bool(row["telegram_bot_token_enc"] and row["telegram_chat_id_enc"]),
        "alert_policy": normalize_alert_policy(json.loads(row["alert_policy_json"] or "{}")),
        "snapshot_count": int(snapshot_count),
        "last_success_at": row["last_success_at"],
        "last_error": row["last_error"],
    }


def save_monitor_settings(
    user_id: int,
    *,
    environment: str = "live",
    enabled: bool = False,
    api_key: str | None = None,
    api_secret: str | None = None,
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,
    alert_policy: dict[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Save supplied secrets while retaining existing values for ``None``.

    Empty strings explicitly remove a credential; this lets the UI provide
    separate delete actions without ever rendering the stored secret.
    """
    environment = str(environment or "live").strip().lower()
    if environment not in {"live", "demo"}:
        raise ValueError("Trading 212 environment must be live or demo.")
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM monitor_accounts WHERE user_id=?", (int(user_id),)).fetchone()
        existing = dict(row) if row else {}
        def choose(column: str, supplied: str | None) -> str | None:
            if supplied is None:
                return existing.get(column)
            cleaned = str(supplied).strip()
            return _encrypt(cleaned) if cleaned else None
        policy = normalize_alert_policy(alert_policy if alert_policy is not None else json.loads(existing.get("alert_policy_json") or "{}"))
        encrypted_api_key = choose("api_key_enc", api_key)
        encrypted_api_secret = choose("api_secret_enc", api_secret)
        encrypted_bot_token = choose("telegram_bot_token_enc", telegram_bot_token)
        encrypted_chat_id = choose("telegram_chat_id_enc", telegram_chat_id)
        if enabled and not (encrypted_api_key and encrypted_api_secret):
            raise ValueError("Save both Trading 212 API Key and API Secret before enabling the monitor.")
        if enabled and not (encrypted_bot_token and encrypted_chat_id):
            raise ValueError("Save Telegram Bot Token and Chat ID before enabling alerts.")
        now = _iso(_now())
        conn.execute(
            """
            INSERT INTO monitor_accounts (
                user_id, enabled, environment, api_key_enc, api_secret_enc,
                telegram_bot_token_enc, telegram_chat_id_enc, alert_policy_json,
                updated_at, last_success_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled=excluded.enabled, environment=excluded.environment,
                api_key_enc=excluded.api_key_enc, api_secret_enc=excluded.api_secret_enc,
                telegram_bot_token_enc=excluded.telegram_bot_token_enc,
                telegram_chat_id_enc=excluded.telegram_chat_id_enc,
                alert_policy_json=excluded.alert_policy_json, updated_at=excluded.updated_at
            """,
            (
                int(user_id), int(bool(enabled)), environment,
                encrypted_api_key, encrypted_api_secret,
                encrypted_bot_token, encrypted_chat_id,
                json.dumps(policy, separators=(",", ":")), now,
                existing.get("last_success_at"), existing.get("last_error"),
            ),
        )
    finally:
        conn.close()
    return get_monitor_status(user_id, path=path)


def remove_trading212_credentials(user_id: int, *, path: Path | None = None) -> None:
    _clear_credentials(user_id, ("api_key_enc", "api_secret_enc"), path=path)


def remove_telegram_credentials(user_id: int, *, path: Path | None = None) -> None:
    _clear_credentials(user_id, ("telegram_bot_token_enc", "telegram_chat_id_enc"), path=path)


def _clear_credentials(user_id: int, columns: tuple[str, ...], *, path: Path | None = None) -> None:
    conn = _connect(path)
    try:
        assignments = ", ".join(f"{column}=NULL" for column in columns)
        conn.execute(f"UPDATE monitor_accounts SET {assignments}, enabled=0, updated_at=? WHERE user_id=?", (_iso(_now()), int(user_id)))
    finally:
        conn.close()


def _account_secrets(row: sqlite3.Row) -> dict[str, str]:
    return {
        "api_key": _decrypt(row["api_key_enc"]),
        "api_secret": _decrypt(row["api_secret_enc"]),
        "telegram_bot_token": _decrypt(row["telegram_bot_token_enc"]),
        "telegram_chat_id": _decrypt(row["telegram_chat_id_enc"]),
    }


def _base_url(environment: str) -> str:
    return "https://demo.trading212.com/api/v0" if environment == "demo" else "https://live.trading212.com/api/v0"


def fetch_positions(
    api_key: str, api_secret: str, environment: str = "live", *, request_get: Callable[..., Any] = requests.get,
    timeout_seconds: int | None = None,
) -> list[dict[str, Any]]:
    timeout = timeout_seconds or max(3, int(os.environ.get("TRADING212_REQUEST_TIMEOUT_SECONDS", "20")))
    try:
        response = request_get(
            f"{_base_url(environment)}/equity/positions",
            auth=(api_key, api_secret), timeout=timeout,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        raise Trading212APIError(f"Trading 212 connection failed: {type(exc).__name__}") from exc
    if response.status_code == 401:
        raise Trading212APIError("Trading 212 rejected the configured read-only credentials.")
    if response.status_code == 429:
        raise Trading212APIError("Trading 212 rate limit reached; the monitor will retry next minute.")
    if response.status_code >= 400:
        raise Trading212APIError(f"Trading 212 request failed (HTTP {response.status_code}).")
    try:
        payload = response.json()
    except ValueError as exc:
        raise Trading212APIError("Trading 212 returned an invalid response.") from exc
    if not isinstance(payload, list):
        raise Trading212APIError("Trading 212 positions response had an unexpected format.")
    return [item for item in payload if isinstance(item, dict)]


def test_trading212_connection(user_id: int, *, path: Path | None = None, request_get: Callable[..., Any] = requests.get) -> int:
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM monitor_accounts WHERE user_id=?", (int(user_id),)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise MonitorConfigurationError("Save Trading 212 API credentials first.")
    secrets = _account_secrets(row)
    if not secrets["api_key"] or not secrets["api_secret"]:
        raise MonitorConfigurationError("Both Trading 212 API Key and API Secret are required.")
    return len(fetch_positions(secrets["api_key"], secrets["api_secret"], row["environment"], request_get=request_get))


def _position_from_api(raw: dict[str, Any]) -> dict[str, Any] | None:
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), dict) else {}
    ticker = str(raw.get("ticker") or instrument.get("ticker") or "").strip()
    try:
        price = float(raw.get("currentPrice"))
    except (TypeError, ValueError):
        return None
    if not ticker or price <= 0:
        return None
    try:
        quantity = float(raw.get("quantity"))
    except (TypeError, ValueError):
        quantity = None
    impact = raw.get("walletImpact") if isinstance(raw.get("walletImpact"), dict) else {}
    try:
        current_value = float(impact.get("currentValue"))
    except (TypeError, ValueError):
        current_value = None
    return {
        "position_key": ticker,
        "ticker": ticker,
        "display_name": str(instrument.get("name") or ticker)[:200],
        "currency": str(instrument.get("currency") or impact.get("currency") or "").upper()[:12],
        "current_price": price,
        "quantity": quantity,
        "current_value": current_value,
    }


def _monitor_clock(value: dt.datetime) -> int:
    """Return a monotonically increasing Berlin weekday-only clock in seconds.

    Saturday and Sunday are frozen.  The Friday close therefore remains the
    immediately preceding monitoring state when the worker resumes on Monday.
    DST changes happen on Sunday in this timezone and consequently do not
    distort an active weekday interval.
    """
    local = value.astimezone(BERLIN_TZ)
    epoch_monday = dt.date(1970, 1, 5)
    monday = local.date() - dt.timedelta(days=local.weekday())
    full_weeks = (monday - epoch_monday).days // 7
    weekday_seconds = min(local.weekday(), 5) * 86_400
    if local.weekday() < 5:
        weekday_seconds += local.hour * 3_600 + local.minute * 60 + local.second
    return full_weeks * 5 * 86_400 + weekday_seconds


def _clock_tolerance_seconds(minutes: int) -> int:
    return max(45, min(3_600, int(minutes * 0.25 * 60)))


def _baseline(conn: sqlite3.Connection, user_id: int, position_key: str, minutes: int, now: dt.datetime) -> sqlite3.Row | None:
    target_clock = _monitor_clock(now) - minutes * 60
    tolerance = _clock_tolerance_seconds(minutes)
    return conn.execute(
        """
        SELECT captured_at, current_price FROM position_snapshots
        WHERE user_id=? AND position_key=? AND monitor_clock >= ? AND monitor_clock <= ?
        ORDER BY ABS(monitor_clock - ?) ASC
        LIMIT 1
        """,
        (int(user_id), position_key, target_clock - tolerance, target_clock + tolerance, target_clock),
    ).fetchone()


def _evaluate_alerts(
    conn: sqlite3.Connection, user_id: int, position: dict[str, Any], now: dt.datetime, policy: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for minutes in WINDOWS_MINUTES:
        key = str(minutes)
        if not policy["windows"].get(key):
            continue
        baseline = _baseline(conn, user_id, position["position_key"], minutes, now)
        if baseline is None:
            continue
        previous = float(baseline["current_price"])
        if previous <= 0:
            continue
        change_pct = (float(position["current_price"]) - previous) / previous * 100
        direction = "up" if change_pct >= 0 else "down"
        for threshold in policy["thresholds"].get(key, []):
            # Rearm both directions only after the move genuinely subsides.
            # Otherwise an up alert followed by a small negative change would
            # leave the prior up state permanently active.
            if abs(change_pct) < float(threshold) * REARM_RATIO:
                conn.execute(
                    "UPDATE alert_state SET is_active=0 WHERE user_id=? AND position_key=? AND window_minutes=? AND threshold_pct=?",
                    (int(user_id), position["position_key"], minutes, float(threshold)),
                )
            crossed = abs(change_pct) >= float(threshold)
            state = conn.execute(
                "SELECT is_active FROM alert_state WHERE user_id=? AND position_key=? AND window_minutes=? AND threshold_pct=? AND direction=?",
                (int(user_id), position["position_key"], minutes, float(threshold), direction),
            ).fetchone()
            active = bool(state and state[0])
            if crossed and not active:
                conn.execute(
                    """
                    INSERT INTO alert_state(user_id, position_key, window_minutes, threshold_pct, direction, is_active, last_triggered_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(user_id, position_key, window_minutes, threshold_pct, direction)
                    DO UPDATE SET is_active=1, last_triggered_at=excluded.last_triggered_at
                    """,
                    (int(user_id), position["position_key"], minutes, float(threshold), direction, _iso(now)),
                )
                events.append({
                    **position, "window_minutes": minutes, "threshold_pct": float(threshold),
                    "change_pct": change_pct, "baseline_price": previous, "baseline_at": baseline["captured_at"],
                    "direction": direction,
                    "resumed_after_pause": (
                        (_parse_iso(baseline["captured_at"]) is not None)
                        and (now - _parse_iso(baseline["captured_at"])).total_seconds()
                        > (minutes * 60 + _clock_tolerance_seconds(minutes))
                    ),
                })
    return events


def _telegram_message(events: list[dict[str, Any]], now: dt.datetime) -> str:
    lines = [f"Trading 212 position alert · {now.astimezone(BERLIN_TZ):%Y-%m-%d %H:%M %Z}"]
    for event in events:
        window = "4h" if event["window_minutes"] == 240 else "24h" if event["window_minutes"] == 1440 else f"{event['window_minutes']}m"
        arrow = "▲" if event["direction"] == "up" else "▼"
        resumed = " · resumed after pause" if event.get("resumed_after_pause") else ""
        lines.append(
            f"{arrow} {event['ticker']} · {window} {event['change_pct']:+.2f}% "
            f"(threshold {event['threshold_pct']:.0f}%) · {event['current_price']:.4g} {event.get('currency') or ''}{resumed}"
        )
    return "\n".join(lines)


def send_telegram_message(
    bot_token: str, chat_id: str, text: str, *, request_post: Callable[..., Any] = requests.post,
) -> None:
    if not bot_token or not chat_id:
        raise MonitorConfigurationError("Telegram Bot Token and Chat ID are required for alerts.")
    try:
        response = request_post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=max(3, int(os.environ.get("TELEGRAM_REQUEST_TIMEOUT_SECONDS", "15"))),
        )
    except requests.RequestException as exc:
        raise Trading212APIError(f"Telegram delivery failed: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise Trading212APIError(f"Telegram delivery failed (HTTP {response.status_code}).")


def send_telegram_test(user_id: int, *, path: Path | None = None, request_post: Callable[..., Any] = requests.post) -> None:
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM monitor_accounts WHERE user_id=?", (int(user_id),)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise MonitorConfigurationError("Save Telegram settings first.")
    secrets = _account_secrets(row)
    send_telegram_message(secrets["telegram_bot_token"], secrets["telegram_chat_id"], "Trading 212 monitor test: Telegram alerts are connected.", request_post=request_post)


def is_monitoring_window(now: dt.datetime | None = None) -> bool:
    """Run Monday-Friday in Berlin time; exchange holidays simply produce no price changes."""
    return (now or _now()).astimezone(BERLIN_TZ).weekday() < 5


def _write_account_status(conn: sqlite3.Connection, user_id: int, *, success: bool, error: str | None = None) -> None:
    if success:
        conn.execute("UPDATE monitor_accounts SET last_success_at=?, last_error=NULL WHERE user_id=?", (_iso(_now()), int(user_id)))
    else:
        conn.execute("UPDATE monitor_accounts SET last_error=? WHERE user_id=?", (_safe_error(error or "monitoring failed"), int(user_id)))


def prune_expired_snapshots(*, path: Path | None = None, now: dt.datetime | None = None) -> int:
    """Enforce a rolling 48 active-monitor-hour window.

    Weekend seconds are frozen rather than counted, so Friday's final state is
    retained as Monday's continuity baseline.
    """
    current_time = (now or _now()).astimezone(UTC).replace(microsecond=0)
    conn = _connect(path)
    try:
        cursor = conn.execute(
            "DELETE FROM position_snapshots WHERE monitor_clock IS NOT NULL AND monitor_clock < ?",
            (_monitor_clock(current_time) - 48 * 3_600,),
        )
        return max(0, int(cursor.rowcount))
    finally:
        conn.close()


def run_account_monitor(
    user_id: int, *, path: Path | None = None, now: dt.datetime | None = None,
    request_get: Callable[..., Any] = requests.get, request_post: Callable[..., Any] = requests.post,
) -> dict[str, Any]:
    """Fetch, retain and evaluate one enabled account. Never leaks credentials."""
    current_time = (now or _now()).astimezone(UTC).replace(microsecond=0)
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM monitor_accounts WHERE user_id=?", (int(user_id),)).fetchone()
        if row is None or not row["enabled"]:
            return {"status": "disabled", "events": []}
        if not is_monitoring_window(current_time):
            return {"status": "weekend_paused", "events": []}
        secrets = _account_secrets(row)
        if not secrets["api_key"] or not secrets["api_secret"]:
            raise MonitorConfigurationError("Trading 212 API Key and API Secret are required.")
        if not secrets["telegram_bot_token"] or not secrets["telegram_chat_id"]:
            raise MonitorConfigurationError("Telegram Bot Token and Chat ID are required.")
        positions = [_position_from_api(item) for item in fetch_positions(
            secrets["api_key"], secrets["api_secret"], row["environment"], request_get=request_get
        )]
        positions = [item for item in positions if item is not None]
        policy = normalize_alert_policy(json.loads(row["alert_policy_json"] or "{}"))
        conn.execute("BEGIN IMMEDIATE")
        try:
            events: list[dict[str, Any]] = []
            for position in positions:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO position_snapshots(
                        user_id, captured_at, position_key, ticker, display_name, currency,
                        current_price, quantity, current_value, monitor_clock
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(user_id), _iso(current_time), position["position_key"], position["ticker"],
                        position["display_name"], position["currency"], position["current_price"],
                        position["quantity"], position["current_value"], _monitor_clock(current_time),
                    ),
                )
                events.extend(_evaluate_alerts(conn, int(user_id), position, current_time, policy))
            cutoff_clock = _monitor_clock(current_time) - 48 * 3_600
            conn.execute(
                "DELETE FROM position_snapshots WHERE user_id=? AND monitor_clock IS NOT NULL AND monitor_clock < ?",
                (int(user_id), cutoff_clock),
            )
            # A delivery failure must not mark an alert tier as active.  Keeping
            # the send inside this small, isolated monitor transaction means a
            # failed Telegram call is retried next minute rather than silently
            # losing the first threshold-crossing notification.
            if events:
                send_telegram_message(
                    secrets["telegram_bot_token"], secrets["telegram_chat_id"],
                    _telegram_message(events, current_time), request_post=request_post,
                )
            _write_account_status(conn, int(user_id), success=True)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"status": "ok", "positions": len(positions), "events": events}
    except (MonitorConfigurationError, Trading212APIError) as exc:
        try:
            _write_account_status(conn, int(user_id), success=False, error=str(exc))
        except sqlite3.Error:
            pass
        return {"status": "error", "error": _safe_error(exc), "events": []}
    finally:
        conn.close()


def run_all_monitors(*, path: Path | None = None) -> list[dict[str, Any]]:
    # The worker still wakes on weekends solely to maintain the frozen active-
    # monitoring retention window; no Trading 212 request is made by paused
    # accounts.
    prune_expired_snapshots(path=path)
    conn = _connect(path)
    try:
        user_ids = [int(row[0]) for row in conn.execute("SELECT user_id FROM monitor_accounts WHERE enabled=1").fetchall()]
    finally:
        conn.close()
    return [run_account_monitor(user_id, path=path) for user_id in user_ids]
