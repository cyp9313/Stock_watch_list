"""Account-scoped, read-only Trading 212 position monitoring.

This module deliberately owns a separate SQLite database.  The Streamlit
application, market-data cache and report queue must never contend with the
minute-level monitor writer.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken

from ticker_mapping import normalize_yfinance_ticker


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
EMA_PERIODS = (20, 50, 100, 200, 250)
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9
RSI_PERIOD = 14
RSI_ALERT_LEVELS = (30.0, 70.0)
VOLUME_AVERAGE_DAYS = 20
VOLUME_CHECK_SECONDS = 5 * 60
VOLUME_SURGE_DEFAULT_RATIO = 2.0

# Trading 212 position tickers commonly use ``SYMBOL_MARKET_EQ``.  This map
# deliberately covers the exchange tags that have an unambiguous yfinance
# suffix.  Other symbols remain safely unmapped instead of guessing a listing.
T212_MARKET_TO_YF_SUFFIX = {
    "US": "", "L": ".L", "LSE": ".L", "DE": ".DE", "F": ".F",
    "PA": ".PA", "AS": ".AS", "BR": ".BR", "MI": ".MI", "SW": ".SW",
    "ST": ".ST", "CO": ".CO", "HE": ".HE", "OL": ".OL", "MC": ".MC",
    "LS": ".LS", "HK": ".HK", "KS": ".KS", "KQ": ".KQ",
}

# Volume is only meaningful while the primary exchange is in its regular
# session. yfinance's daily volume is not a reliable measure of Trading 212's
# overnight/extended-hours activity, so those sessions retain price monitoring
# but deliberately skip volume-surge alerts.
T212_REGULAR_SESSIONS = {
    "US": (ZoneInfo("America/New_York"), ((9 * 60 + 30, 16 * 60),)),
    "L": (ZoneInfo("Europe/London"), ((8 * 60, 16 * 60 + 30),)),
    "LSE": (ZoneInfo("Europe/London"), ((8 * 60, 16 * 60 + 30),)),
    "DE": (BERLIN_TZ, ((9 * 60, 17 * 60 + 30),)),
    "F": (BERLIN_TZ, ((9 * 60, 17 * 60 + 30),)),
    "PA": (ZoneInfo("Europe/Paris"), ((9 * 60, 17 * 60 + 30),)),
    "AS": (ZoneInfo("Europe/Amsterdam"), ((9 * 60, 17 * 60 + 30),)),
    "BR": (ZoneInfo("Europe/Brussels"), ((9 * 60, 17 * 60 + 30),)),
    "MI": (ZoneInfo("Europe/Rome"), ((9 * 60, 17 * 60 + 30),)),
    "MC": (ZoneInfo("Europe/Madrid"), ((9 * 60, 17 * 60 + 30),)),
    "HK": (ZoneInfo("Asia/Hong_Kong"), ((9 * 60 + 30, 12 * 60), (13 * 60, 16 * 60))),
    "KS": (ZoneInfo("Asia/Seoul"), ((9 * 60, 15 * 60 + 30),)),
    "KQ": (ZoneInfo("Asia/Seoul"), ((9 * 60, 15 * 60 + 30),)),
}


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS instrument_mappings (
            position_key TEXT PRIMARY KEY,
            trading212_ticker TEXT NOT NULL,
            display_name TEXT,
            isin TEXT,
            exchange_code TEXT,
            currency TEXT,
            yfinance_ticker TEXT,
            mapping_status TEXT NOT NULL DEFAULT 'pending',
            mapping_source TEXT,
            last_history_check_date TEXT,
            last_volume_history_check_date TEXT,
            last_history_date TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    mapping_columns = {row[1] for row in conn.execute("PRAGMA table_info(instrument_mappings)").fetchall()}
    if "last_volume_history_check_date" not in mapping_columns:
        conn.execute("ALTER TABLE instrument_mappings ADD COLUMN last_volume_history_check_date TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_price_history (
            yfinance_ticker TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            close_price REAL NOT NULL,
            volume REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(yfinance_ticker, trading_date)
        )
        """
    )
    daily_price_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_price_history)").fetchall()}
    if "volume" not in daily_price_columns:
        conn.execute("ALTER TABLE daily_price_history ADD COLUMN volume REAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS intraday_volume_checks (
            yfinance_ticker TEXT PRIMARY KEY,
            checked_at TEXT NOT NULL,
            trading_date TEXT,
            volume REAL,
            last_error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS volume_alert_state (
            user_id INTEGER NOT NULL,
            position_key TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            ratio REAL NOT NULL,
            triggered_at TEXT NOT NULL,
            PRIMARY KEY(user_id, position_key, trading_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_observed_positions (
            user_id INTEGER NOT NULL,
            position_key TEXT NOT NULL,
            ticker TEXT NOT NULL,
            display_name TEXT,
            currency TEXT,
            last_observed_at TEXT NOT NULL,
            PRIMARY KEY(user_id, position_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS technical_signal_state (
            user_id INTEGER NOT NULL,
            position_key TEXT NOT NULL,
            signal_key TEXT NOT NULL,
            last_value REAL NOT NULL,
            last_side TEXT,
            last_observed_at TEXT NOT NULL,
            last_triggered_at TEXT,
            PRIMARY KEY(user_id, position_key, signal_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_price_history_ticker_date "
        "ON daily_price_history(yfinance_ticker, trading_date DESC)"
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
        "technical": {
            "ema_crosses": True,
            "macd_crosses": True,
            "rsi_levels": True,
            "volume_surge": True,
        },
        "volume": {"relative_ratio_threshold": VOLUME_SURGE_DEFAULT_RATIO},
    }


def normalize_alert_policy(raw: Any) -> dict[str, Any]:
    defaults = default_alert_policy()
    raw = raw if isinstance(raw, dict) else {}
    raw_windows = raw.get("windows") if isinstance(raw.get("windows"), dict) else {}
    raw_thresholds = raw.get("thresholds") if isinstance(raw.get("thresholds"), dict) else {}
    raw_technical = raw.get("technical") if isinstance(raw.get("technical"), dict) else {}
    raw_volume = raw.get("volume") if isinstance(raw.get("volume"), dict) else {}
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
    technical = {
        key: bool(raw_technical.get(key, defaults["technical"][key]))
        for key in defaults["technical"]
    }
    try:
        volume_ratio = float(raw_volume.get("relative_ratio_threshold", defaults["volume"]["relative_ratio_threshold"]))
    except (TypeError, ValueError):
        volume_ratio = defaults["volume"]["relative_ratio_threshold"]
    volume_ratio = min(10.0, max(1.1, volume_ratio))
    return {
        "windows": windows, "thresholds": thresholds, "technical": technical,
        "volume": {"relative_ratio_threshold": volume_ratio},
    }


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


def list_position_mappings(user_id: int, *, path: Path | None = None) -> list[dict[str, Any]]:
    """Return only the caller's observed holdings and their safe mapping metadata."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            WITH positions AS (
                SELECT position_key, ticker, display_name, currency
                FROM user_observed_positions WHERE user_id=?
                UNION
                SELECT s.position_key, s.ticker, s.display_name, s.currency
                FROM position_snapshots s
                WHERE s.user_id=?
                  AND s.captured_at=(
                      SELECT MAX(other.captured_at) FROM position_snapshots other
                      WHERE other.user_id=s.user_id AND other.position_key=s.position_key
                  )
            )
            SELECT p.position_key, p.ticker, p.display_name, p.currency,
                   m.yfinance_ticker, m.mapping_status, m.mapping_source,
                   m.last_history_date, m.last_error
            FROM positions p
            LEFT JOIN instrument_mappings m ON m.position_key=p.position_key
            ORDER BY p.ticker COLLATE NOCASE
            """,
            (int(user_id), int(user_id)),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def save_yfinance_mapping_override(
    user_id: int,
    position_key: str,
    yfinance_ticker: str,
    *,
    path: Path | None = None,
) -> None:
    """Save a user-approved mapping only for a holding observed in that account."""
    normalized = normalize_yfinance_ticker(yfinance_ticker)
    if not normalized or not re.fullmatch(r"[A-Z0-9.\-]+", normalized):
        raise ValueError("Enter a valid yfinance ticker, for example AAPL, VUSA.L, or 0700.HK.")
    conn = _connect(path)
    try:
        snapshot = conn.execute(
            """
            SELECT position_key, ticker, display_name, currency
            FROM user_observed_positions WHERE user_id=? AND position_key=?
            """,
            (int(user_id), str(position_key)),
        ).fetchone()
        if snapshot is None:
            snapshot = conn.execute(
                """
                SELECT position_key, ticker, display_name, currency
                FROM position_snapshots WHERE user_id=? AND position_key=?
                ORDER BY captured_at DESC LIMIT 1
                """,
                (int(user_id), str(position_key)),
            ).fetchone()
        if snapshot is None:
            raise ValueError("This holding has not been observed for your account yet.")
        existing = conn.execute(
            "SELECT isin, exchange_code FROM instrument_mappings WHERE position_key=?", (str(position_key),)
        ).fetchone()
        position = {
            "position_key": snapshot["position_key"], "ticker": snapshot["ticker"],
            "display_name": snapshot["display_name"], "currency": snapshot["currency"],
            "isin": existing["isin"] if existing else "",
            "exchange_code": existing["exchange_code"] if existing else "",
        }
        _upsert_mapping(
            conn, position, yfinance_ticker=normalized, mapping_status="manual",
            mapping_source="manual", last_history_check_date=None,
            last_volume_history_check_date=None, last_history_date=None, last_error=None,
        )
    finally:
        conn.close()


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
        "isin": str(instrument.get("isin") or raw.get("isin") or "").upper()[:32],
        "exchange_code": str(instrument.get("exchange") or raw.get("exchange") or "").upper()[:32],
        "current_price": price,
        "quantity": quantity,
        "current_value": current_value,
    }


def _record_observed_position(
    conn: sqlite3.Connection, user_id: int, position: dict[str, Any], now: dt.datetime,
) -> None:
    """Keep a non-price position identity for UI mapping controls."""
    conn.execute(
        """
        INSERT INTO user_observed_positions(user_id, position_key, ticker, display_name, currency, last_observed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, position_key) DO UPDATE SET
            ticker=excluded.ticker, display_name=excluded.display_name,
            currency=excluded.currency, last_observed_at=excluded.last_observed_at
        """,
        (
            int(user_id), position["position_key"], position["ticker"],
            position.get("display_name") or "", position.get("currency") or "", _iso(now),
        ),
    )


def _t212_yfinance_candidates(position: dict[str, Any]) -> list[str]:
    """Generate conservative, ordered yfinance candidates for a T212 position."""
    raw = str(position.get("ticker") or "").strip().upper()
    if not raw:
        return []
    candidates: list[str] = []
    match = re.fullmatch(r"(.+)_([A-Z0-9]+)_(?:EQ|ETF)", raw)
    if match:
        symbol, market = match.groups()
        suffix = T212_MARKET_TO_YF_SUFFIX.get(market)
        if suffix is not None:
            candidates.append(f"{symbol}{suffix}")
            # A suffix-less fallback is only safe for the US listing. For an
            # unknown venue it could silently select a different company.
            if market == "US":
                candidates.append(symbol)
    candidates.append(raw)
    exchange = str(position.get("exchange_code") or "").upper()
    symbol = match.group(1) if match else raw
    suffix = T212_MARKET_TO_YF_SUFFIX.get(exchange)
    if suffix is not None:
        candidates.insert(0, f"{symbol}{suffix}")

    normalized: list[str] = []
    for candidate in candidates:
        ticker = normalize_yfinance_ticker(candidate)
        if ticker and ticker not in normalized:
            normalized.append(ticker)
    return normalized


def _download_yfinance_daily_history(yfinance_ticker: str, end_date: dt.date) -> list[tuple[str, float, float | None]]:
    """Download up to two years of completed daily closes, excluding ``end_date``."""
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - dependency is production-required
        raise Trading212APIError("yfinance is not installed for technical monitoring.") from exc
    try:
        frame = yf.Ticker(yfinance_ticker).history(
            period="2y",
            end=end_date.isoformat(),
            auto_adjust=False,
            actions=False,
        )
    except Exception as exc:
        raise Trading212APIError(f"yfinance daily history failed for {yfinance_ticker}: {type(exc).__name__}") from exc
    if frame is None or frame.empty or "Close" not in frame:
        return []
    rows: list[tuple[str, float, float | None]] = []
    for index, value in frame["Close"].items():
        try:
            close = float(value)
            trade_date = index.date().isoformat()
        except (TypeError, ValueError, AttributeError):
            continue
        if close > 0 and trade_date < end_date.isoformat():
            try:
                volume = float(frame.at[index, "Volume"])
            except (KeyError, TypeError, ValueError):
                volume = None
            rows.append((trade_date, close, volume if volume is not None and volume >= 0 else None))
    return rows


def _upsert_mapping(conn: sqlite3.Connection, position: dict[str, Any], **updates: Any) -> None:
    existing = conn.execute(
        "SELECT * FROM instrument_mappings WHERE position_key=?", (position["position_key"],)
    ).fetchone()
    data = dict(existing) if existing else {}
    data.update(updates)
    now = _iso(_now())
    conn.execute(
        """
        INSERT INTO instrument_mappings(
            position_key, trading212_ticker, display_name, isin, exchange_code, currency,
            yfinance_ticker, mapping_status, mapping_source, last_history_check_date,
            last_volume_history_check_date, last_history_date, last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(position_key) DO UPDATE SET
            trading212_ticker=excluded.trading212_ticker, display_name=excluded.display_name,
            isin=excluded.isin, exchange_code=excluded.exchange_code, currency=excluded.currency,
            yfinance_ticker=excluded.yfinance_ticker, mapping_status=excluded.mapping_status,
            mapping_source=excluded.mapping_source, last_history_check_date=excluded.last_history_check_date,
            last_volume_history_check_date=excluded.last_volume_history_check_date,
            last_history_date=excluded.last_history_date, last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (
            position["position_key"], position["ticker"], position.get("display_name") or "",
            position.get("isin") or "", position.get("exchange_code") or "", position.get("currency") or "",
            data.get("yfinance_ticker"), data.get("mapping_status", "pending"), data.get("mapping_source"),
            data.get("last_history_check_date"), data.get("last_volume_history_check_date"),
            data.get("last_history_date"), data.get("last_error"), now,
        ),
    )


def mapped_yfinance_ticker(
    conn: sqlite3.Connection,
    position_key: str,
    *,
    fallback: str = "",
) -> str:
    """Return the verified display/search ticker without replacing broker identity.

    Trading 212 instrument names and symbols can lag a corporate rename.
    Snapshots and monitor state retain the broker's raw identifier, whereas
    user-facing messages may use a yfinance mapping only after it was verified
    from daily history or confirmed manually by the account owner.
    """
    row = conn.execute(
        "SELECT yfinance_ticker, mapping_status FROM instrument_mappings WHERE position_key=?",
        (str(position_key),),
    ).fetchone()
    if row is not None and str(row["mapping_status"] or "") in {"mapped", "manual"}:
        ticker = normalize_yfinance_ticker(str(row["yfinance_ticker"] or ""))
        if ticker:
            return ticker
    return str(fallback or "").strip()


def _position_with_report_ticker(
    conn: sqlite3.Connection,
    position: dict[str, Any],
    *,
    yfinance_ticker: str | None = None,
) -> dict[str, Any]:
    """Attach a presentation ticker while leaving the raw broker ticker intact."""
    return {
        **position,
        "report_ticker": normalize_yfinance_ticker(yfinance_ticker or "")
        or mapped_yfinance_ticker(conn, position["position_key"], fallback=position["ticker"]),
    }


def _cached_daily_closes(conn: sqlite3.Connection, yfinance_ticker: str) -> list[float]:
    rows = conn.execute(
        "SELECT close_price FROM daily_price_history WHERE yfinance_ticker=? ORDER BY trading_date ASC",
        (yfinance_ticker,),
    ).fetchall()
    return [float(row[0]) for row in rows if float(row[0]) > 0]


def _cached_average_daily_volume(conn: sqlite3.Connection, yfinance_ticker: str) -> float | None:
    rows = conn.execute(
        """
        SELECT volume FROM daily_price_history
        WHERE yfinance_ticker=? AND volume IS NOT NULL AND volume > 0
        ORDER BY trading_date DESC LIMIT ?
        """,
        (yfinance_ticker, VOLUME_AVERAGE_DAYS),
    ).fetchall()
    values = [float(row[0]) for row in rows if row[0] is not None and float(row[0]) > 0]
    return sum(values) / len(values) if len(values) >= VOLUME_AVERAGE_DAYS else None


def _position_market(position: dict[str, Any]) -> str | None:
    raw = str(position.get("ticker") or "").strip().upper()
    match = re.fullmatch(r".+_([A-Z0-9]+)_(?:EQ|ETF)", raw)
    market = match.group(1) if match else str(position.get("exchange_code") or "").strip().upper()
    return market if market in T212_REGULAR_SESSIONS else None


def _regular_session_progress(position: dict[str, Any], now: dt.datetime) -> tuple[str, float] | None:
    """Return local trade date and completed regular-session fraction, if open."""
    market = _position_market(position)
    if not market:
        return None
    timezone, sessions = T212_REGULAR_SESSIONS[market]
    local = now.astimezone(timezone)
    if local.weekday() >= 5:
        return None
    current_minute = local.hour * 60 + local.minute + local.second / 60
    total_minutes = sum(end - start for start, end in sessions)
    elapsed = 0.0
    is_open = False
    for start, end in sessions:
        if start <= current_minute < end:
            elapsed += current_minute - start
            is_open = True
            break
        if current_minute >= end:
            elapsed += end - start
    # The first 30 minutes are intentionally ignored: cumulative volume is too
    # noisy there and can create misleading opening-auction alerts.
    if not is_open or elapsed < 30 or total_minutes <= 0:
        return None
    return local.date().isoformat(), min(1.0, elapsed / total_minutes)


def _download_yfinance_current_volume(yfinance_ticker: str) -> float | None:
    """Read yfinance's current daily cumulative volume, never intraday bars."""
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - dependency is production-required
        raise Trading212APIError("yfinance is not installed for volume monitoring.") from exc
    try:
        frame = yf.Ticker(yfinance_ticker).history(
            period="1d", interval="1d", auto_adjust=False, actions=False,
        )
    except Exception as exc:
        raise Trading212APIError(f"yfinance current volume failed for {yfinance_ticker}: {type(exc).__name__}") from exc
    if frame is None or frame.empty or "Volume" not in frame:
        return None
    try:
        volume = float(frame["Volume"].iloc[-1])
    except (TypeError, ValueError, IndexError):
        return None
    return volume if volume >= 0 else None


def _current_intraday_volume(
    conn: sqlite3.Connection,
    position: dict[str, Any],
    yfinance_ticker: str,
    now: dt.datetime,
    *,
    volume_fetcher: Callable[[str], float | None] = _download_yfinance_current_volume,
) -> tuple[float | None, float | None, str | None, str | None]:
    """Return current volume, session fraction and an optional safe error."""
    session = _regular_session_progress(position, now)
    if session is None:
        return None, None, None, None
    trade_date, fraction = session
    cached = conn.execute(
        "SELECT checked_at, trading_date, volume, last_error FROM intraday_volume_checks WHERE yfinance_ticker=?",
        (yfinance_ticker,),
    ).fetchone()
    checked_at = _parse_iso(cached["checked_at"]) if cached else None
    recent = checked_at is not None and (now - checked_at).total_seconds() < VOLUME_CHECK_SECONDS
    if cached and recent and cached["trading_date"] == trade_date:
        return (
            float(cached["volume"]) if cached["volume"] is not None else None,
            fraction, trade_date, cached["last_error"],
        )
    try:
        volume = volume_fetcher(yfinance_ticker)
        error = None if volume is not None else "intraday_volume_unavailable"
    except Exception as exc:
        volume, error = None, _safe_error(exc)
    conn.execute(
        """
        INSERT INTO intraday_volume_checks(yfinance_ticker, checked_at, trading_date, volume, last_error)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(yfinance_ticker) DO UPDATE SET
            checked_at=excluded.checked_at, trading_date=excluded.trading_date,
            volume=excluded.volume, last_error=excluded.last_error
        """,
        (yfinance_ticker, _iso(now), trade_date, volume, error),
    )
    return volume, fraction, trade_date, error


def _history_refresh_key(now: dt.datetime) -> tuple[str, dt.date]:
    """Choose a safe daily-history cutoff without accepting an open US session.

    The broker worker runs in Berlin time while a position can be listed in a
    different market. Before 06:00 Berlin we deliberately exclude the prior
    calendar day: that prevents a still-open US session from being mistaken for
    a completed yfinance daily bar. A newly seen holding may be fetched at
    that safe cutoff immediately; the normal daily refresh will then happen
    after 06:00 Berlin.
    """
    local = now.astimezone(BERLIN_TZ)
    today = local.date()
    if local.hour < 6:
        return (f"{today.isoformat()}:preclose", today - dt.timedelta(days=1))
    return (today.isoformat(), today)


def _ensure_daily_history(
    conn: sqlite3.Connection,
    position: dict[str, Any],
    now: dt.datetime,
    *,
    history_fetcher: Callable[[str, dt.date], list[tuple[Any, ...]]] = _download_yfinance_daily_history,
) -> tuple[str | None, list[float], str | None]:
    """Return completed daily closes, updating a ticker at most once per Berlin day."""
    mapping = conn.execute(
        "SELECT * FROM instrument_mappings WHERE position_key=?", (position["position_key"],)
    ).fetchone()
    mapping_data = dict(mapping) if mapping else {}
    yfinance_ticker = str(mapping_data.get("yfinance_ticker") or "").strip()
    refresh_key, end_date = _history_refresh_key(now)
    checked_today = str(mapping_data.get("last_history_check_date") or "") == refresh_key
    volume_checked_today = str(mapping_data.get("last_volume_history_check_date") or "") == refresh_key
    if checked_today:
        if yfinance_ticker:
            closes = _cached_daily_closes(conn, yfinance_ticker)
            # Older monitor databases contain close-only history. Bootstrap
            # volume columns once without turning every minute into a retry.
            if closes and (_cached_average_daily_volume(conn, yfinance_ticker) is not None or volume_checked_today):
                return yfinance_ticker, closes, None
            if not closes:
                return yfinance_ticker, closes, "daily_history_empty"
        if not yfinance_ticker:
            return None, [], str(mapping_data.get("last_error") or "no_yfinance_candidate")
    # Existing mappings wait for the normal post-close refresh.  A new holding
    # (which has no mapped ticker yet) is the documented exception: it may
    # fetch immediately from the last safe completed-bar cutoff.
    if yfinance_ticker and now.astimezone(BERLIN_TZ).hour < 6:
        closes = _cached_daily_closes(conn, yfinance_ticker)
        if closes:
            return yfinance_ticker, closes, None

    candidates = [yfinance_ticker] if yfinance_ticker else _t212_yfinance_candidates(position)
    candidates = [candidate for candidate in candidates if candidate]
    last_error = "no_yfinance_candidate" if not candidates else "daily_history_unavailable"
    for candidate in candidates:
        try:
            rows = history_fetcher(candidate, end_date)
        except Exception as exc:
            last_error = _safe_error(exc)
            continue
        if not rows:
            last_error = "daily_history_empty"
            continue
        conn.execute("DELETE FROM daily_price_history WHERE yfinance_ticker=?", (candidate,))
        normalized_rows: list[tuple[str, float, float | None]] = []
        for item in rows:
            try:
                trade_date, close = str(item[0]), float(item[1])
                volume = float(item[2]) if len(item) > 2 and item[2] is not None else None
            except (IndexError, TypeError, ValueError):
                continue
            if close > 0:
                normalized_rows.append((trade_date, close, volume if volume is None or volume >= 0 else None))
        if not normalized_rows:
            last_error = "daily_history_empty"
            continue
        conn.executemany(
            "INSERT INTO daily_price_history(yfinance_ticker, trading_date, close_price, volume, updated_at) VALUES (?, ?, ?, ?, ?)",
            [(candidate, trade_date, close, volume, _iso(now)) for trade_date, close, volume in normalized_rows],
        )
        _upsert_mapping(
            conn, position, yfinance_ticker=candidate,
            mapping_status="manual" if mapping_data.get("mapping_source") == "manual" else "mapped",
            mapping_source="manual" if mapping_data.get("mapping_source") == "manual" else (
                "cached" if yfinance_ticker else "trading212_ticker_candidate"
            ),
            last_history_check_date=refresh_key, last_volume_history_check_date=refresh_key,
            last_history_date=normalized_rows[-1][0], last_error=None,
        )
        return candidate, [close for _, close, _ in normalized_rows], None

    manual = mapping_data.get("mapping_source") == "manual"
    _upsert_mapping(
        conn, position, yfinance_ticker=yfinance_ticker or None,
        mapping_status="manual_error" if manual else "unmapped",
        mapping_source="manual" if manual else None,
        last_history_check_date=refresh_key, last_volume_history_check_date=refresh_key,
        last_history_date=mapping_data.get("last_history_date"),
        last_error=last_error,
    )
    return None, [], last_error


def refresh_position_mappings(
    user_id: int,
    *,
    path: Path | None = None,
    now: dt.datetime | None = None,
    request_get: Callable[..., Any] = requests.get,
    history_fetcher: Callable[[str, dt.date], list[tuple[Any, ...]]] = _download_yfinance_daily_history,
) -> dict[str, Any]:
    """Manually validate ticker mappings without requiring an active monitor window.

    This is deliberately read-only with respect to broker positions: it writes
    mapping/cache metadata only. It does not add price snapshots, evaluate
    alerts, send Telegram messages, or alter the 5x24 worker schedule.
    """
    current_time = (now or _now()).astimezone(UTC).replace(microsecond=0)
    conn = _connect(path)
    try:
        row = conn.execute("SELECT * FROM monitor_accounts WHERE user_id=?", (int(user_id),)).fetchone()
        if row is None:
            raise MonitorConfigurationError("Save Trading 212 API credentials first.")
        secrets = _account_secrets(row)
        if not secrets["api_key"] or not secrets["api_secret"]:
            raise MonitorConfigurationError("Both Trading 212 API Key and API Secret are required.")
        positions = [
            position for item in fetch_positions(
                secrets["api_key"], secrets["api_secret"], row["environment"], request_get=request_get,
            )
            if (position := _position_from_api(item)) is not None
        ]
        mapped = 0
        unavailable: dict[str, str] = {}
        for position in positions:
            _record_observed_position(conn, int(user_id), position, current_time)
            ticker, closes, error = _ensure_daily_history(
                conn, position, current_time, history_fetcher=history_fetcher,
            )
            if ticker and closes:
                mapped += 1
            elif error:
                unavailable[position["ticker"]] = error
        return {"positions": len(positions), "mapped": mapped, "unavailable": unavailable}
    finally:
        conn.close()


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def _rsi_series(values: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return output
    gains = [max(0.0, values[i] - values[i - 1]) for i in range(1, len(values))]
    losses = [max(0.0, values[i - 1] - values[i]) for i in range(1, len(values))]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    def rsi() -> float:
        if average_loss == 0:
            return 100.0
        rs = average_gain / average_loss
        return 100.0 - 100.0 / (1.0 + rs)
    output[period] = rsi()
    for index in range(period, len(gains)):
        average_gain = (average_gain * (period - 1) + gains[index]) / period
        average_loss = (average_loss * (period - 1) + losses[index]) / period
        output[index + 1] = rsi()
    return output


def _technical_values(daily_closes: list[float], live_price: float) -> dict[str, float]:
    """Calculate daily indicators with T212's latest price as today's temporary close."""
    values = [*daily_closes, float(live_price)]
    result: dict[str, float] = {}
    for period in EMA_PERIODS:
        if len(daily_closes) >= period:
            result[f"ema_{period}"] = _ema_series(values, period)[-1]
    if len(daily_closes) >= MACD_SLOW_PERIOD + MACD_SIGNAL_PERIOD:
        fast = _ema_series(values, MACD_FAST_PERIOD)
        slow = _ema_series(values, MACD_SLOW_PERIOD)
        macd = [a - b for a, b in zip(fast, slow)]
        signal = _ema_series(macd, MACD_SIGNAL_PERIOD)
        result["macd_diff"] = macd[-1] - signal[-1]
    rsi = _rsi_series(values)
    if rsi[-1] is not None:
        result["rsi_14"] = float(rsi[-1])
    return result


def _monitor_clock(value: dt.datetime) -> int:
    """Return a monotonic 5x24 Berlin clock: Mon 02:00 to Sat 02:00.

    The inactive interval is frozen. The Friday/Saturday final state is thus
    retained until monitoring resumes at Monday 02:00, while daylight-saving
    changes fall inside the frozen weekend interval.
    """
    local = value.astimezone(BERLIN_TZ)
    epoch_monday = dt.date(1970, 1, 5)
    monday = local.date() - dt.timedelta(days=local.weekday())
    full_weeks = (monday - epoch_monday).days // 7
    elapsed = (
        local.weekday() * 86_400 + local.hour * 3_600 + local.minute * 60 + local.second - 2 * 3_600
    )
    active_week_seconds = 5 * 86_400
    if elapsed < 0:
        # Monday 00:00-01:59 remains part of the previous frozen weekend.
        full_weeks -= 1
        elapsed = active_week_seconds
    elif elapsed > active_week_seconds:
        elapsed = active_week_seconds
    return full_weeks * active_week_seconds + elapsed


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


def _technical_state_event(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    position: dict[str, Any],
    signal_key: str,
    current_value: float,
    current_side: str | None,
    now: dt.datetime,
    event_kind: str,
    direction: str,
    detail: str,
) -> dict[str, Any] | None:
    """Persist a technical observation and emit only on a genuine state cross."""
    state = conn.execute(
        "SELECT last_value, last_side, last_triggered_at FROM technical_signal_state WHERE user_id=? AND position_key=? AND signal_key=?",
        (int(user_id), position["position_key"], signal_key),
    ).fetchone()
    previous_side = str(state["last_side"] or "") if state is not None else ""
    crossed = bool(state is not None and previous_side and previous_side != str(current_side or ""))
    conn.execute(
        """
        INSERT INTO technical_signal_state(
            user_id, position_key, signal_key, last_value, last_side, last_observed_at, last_triggered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, position_key, signal_key) DO UPDATE SET
            last_value=excluded.last_value, last_side=excluded.last_side,
            last_observed_at=excluded.last_observed_at, last_triggered_at=excluded.last_triggered_at
        """,
        (
            int(user_id), position["position_key"], signal_key, float(current_value), current_side,
            _iso(now), _iso(now) if crossed else (state["last_triggered_at"] if state is not None else None),
        ),
    )
    if not crossed:
        return None
    return {
        **position,
        "event_type": "technical",
        "technical_kind": event_kind,
        "signal_key": signal_key,
        "direction": direction,
        "technical_value": float(current_value),
        "previous_value": float(state["last_value"]),
        "detail": detail,
    }


def _evaluate_technical_alerts(
    conn: sqlite3.Connection,
    user_id: int,
    position: dict[str, Any],
    daily_closes: list[float],
    now: dt.datetime,
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    technical = policy.get("technical", {})
    if not any(bool(technical.get(key)) for key in ("ema_crosses", "macd_crosses", "rsi_levels")):
        return []
    values = _technical_values(daily_closes, float(position["current_price"]))
    events: list[dict[str, Any]] = []
    if technical.get("ema_crosses"):
        for period in EMA_PERIODS:
            ema = values.get(f"ema_{period}")
            if ema is None:
                continue
            distance = float(position["current_price"]) - ema
            side = "above" if distance > 0 else "below"
            direction = "up" if side == "above" else "down"
            event = _technical_state_event(
                conn, user_id=user_id, position=position, signal_key=f"ema_{period}",
                current_value=distance, current_side=side, now=now, event_kind="ema_cross",
                direction=direction,
                detail=f"Price {position['current_price']:.4g} crossed {'above' if direction == 'up' else 'below'} EMA {period} {ema:.4g}",
            )
            if event:
                event["reference_value"] = ema
                event["ema_period"] = period
                events.append(event)
    if technical.get("macd_crosses") and "macd_diff" in values:
        macd_diff = values["macd_diff"]
        side = "positive" if macd_diff > 0 else "negative"
        direction = "golden" if side == "positive" else "death"
        event = _technical_state_event(
            conn, user_id=user_id, position=position, signal_key="macd_diff", current_value=macd_diff,
            current_side=side, now=now, event_kind="macd_cross", direction=direction,
            detail=f"Daily MACD {'golden cross' if direction == 'golden' else 'death cross'} (Diff {macd_diff:+.4g})",
        )
        if event:
            events.append(event)
    if technical.get("rsi_levels") and "rsi_14" in values:
        rsi = values["rsi_14"]
        for level in RSI_ALERT_LEVELS:
            side = "above" if rsi >= level else "below"
            direction = "up" if side == "above" else "down"
            event = _technical_state_event(
                conn, user_id=user_id, position=position, signal_key=f"rsi_{int(level)}", current_value=rsi,
                current_side=side, now=now, event_kind="rsi_level_cross", direction=direction,
                detail=f"Daily RSI(14) {rsi:.1f} crossed {'above' if direction == 'up' else 'below'} {int(level)}",
            )
            if event:
                event["reference_value"] = level
                events.append(event)
    return events


def _evaluate_volume_surge(
    conn: sqlite3.Connection,
    user_id: int,
    position: dict[str, Any],
    *,
    yfinance_ticker: str,
    current_volume: float | None,
    session_fraction: float | None,
    trade_date: str | None,
    policy: dict[str, Any],
    now: dt.datetime,
) -> dict[str, Any] | None:
    """Alert once per exchange day when cumulative volume is unusually high.

    A ratio of 2.0 means today's cumulative volume is at least twice the
    expected cumulative volume at this point in the regular session, where the
    expectation is a 20-completed-day average multiplied by elapsed-session
    fraction. It avoids comparing 10:00 volume with an entire normal day.
    """
    if current_volume is None or session_fraction is None or not trade_date:
        return None
    average_volume = _cached_average_daily_volume(conn, yfinance_ticker)
    if average_volume is None or average_volume <= 0:
        return None
    expected_volume = average_volume * session_fraction
    if expected_volume <= 0:
        return None
    ratio = current_volume / expected_volume
    threshold = float(policy["volume"]["relative_ratio_threshold"])
    if ratio < threshold:
        return None
    existing = conn.execute(
        "SELECT 1 FROM volume_alert_state WHERE user_id=? AND position_key=? AND trading_date=?",
        (int(user_id), position["position_key"], trade_date),
    ).fetchone()
    if existing is not None:
        return None
    conn.execute(
        "INSERT INTO volume_alert_state(user_id, position_key, trading_date, ratio, triggered_at) VALUES (?, ?, ?, ?, ?)",
        (int(user_id), position["position_key"], trade_date, ratio, _iso(now)),
    )
    return {
        **position,
        "event_type": "technical",
        "technical_kind": "volume_surge",
        "direction": "up",
        "technical_value": ratio,
        "reference_value": threshold,
        "detail": (
            f"Regular-session volume {current_volume:,.0f} is {ratio:.2f}x expected "
            f"({session_fraction:.0%} of session; 20D avg {average_volume:,.0f})"
        ),
    }


def _telegram_text(value: Any) -> str:
    """Escape broker-derived values for Telegram's limited HTML dialect."""
    return html.escape(str(value or ""), quote=False)


def _telegram_price(event: dict[str, Any]) -> str:
    price = float(event.get("current_price") or 0.0)
    currency = _telegram_text(event.get("currency"))
    return f"{price:.4g}{(' ' + currency) if currency else ''}"


def _telegram_technical_heading(event: dict[str, Any]) -> tuple[str, str]:
    """Return a visually distinct emoji and short, boldable trigger label."""
    kind = str(event.get("technical_kind") or "")
    direction = str(event.get("direction") or "")
    if kind == "ema_cross":
        try:
            period = int(event.get("ema_period"))
        except (TypeError, ValueError):
            period = 0
        suffix = f" EMA {period}" if period > 0 else " EMA"
        return ("🟢" if direction == "up" else "🔴", f"PRICE CROSSED {'ABOVE' if direction == 'up' else 'BELOW'}{suffix}")
    if kind == "macd_cross":
        return ("🟢" if direction == "golden" else "🔴", f"MACD {'GOLDEN' if direction == 'golden' else 'DEATH'} CROSS")
    if kind == "rsi_level_cross":
        return ("🟢" if direction == "up" else "🔴", f"RSI(14) CROSSED {'UP' if direction == 'up' else 'DOWN'}")
    if kind == "volume_surge":
        return "📊", "UNUSUAL VOLUME SURGE"
    return "⚠️", "TECHNICAL SIGNAL"


def _telegram_message(events: list[dict[str, Any]], now: dt.datetime) -> str:
    """Build a safe, readable Telegram HTML alert without relying on CSS."""
    timestamp = now.astimezone(BERLIN_TZ).strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"🚨 <b>TRADING 212 POSITION ALERT</b>\n🕒 <code>{timestamp}</code>"]
    for event in events:
        ticker = _telegram_text(event.get("report_ticker") or event.get("ticker"))
        detail = _telegram_text(event.get("detail"))
        live_price = _telegram_price(event)
        if event.get("event_type") == "technical":
            icon, heading = _telegram_technical_heading(event)
            lines.append(
                f"{icon} <b>{ticker}</b> · <b>{heading}</b>\n"
                f"↳ {detail}\n"
                f"💵 Live price: <code>{live_price}</code>"
            )
            continue
        window = "4h" if event["window_minutes"] == 240 else "24h" if event["window_minutes"] == 1440 else f"{event['window_minutes']}m"
        upward = event["direction"] == "up"
        icon = "🟢📈" if upward else "🔴📉"
        resumed = " <i>(resumed after pause)</i>" if event.get("resumed_after_pause") else ""
        lines.append(
            f"{icon} <b>{ticker}</b> · <b>{window} MOVE {event['change_pct']:+.2f}%</b>\n"
            f"↳ Trigger: <b>±{event['threshold_pct']:.0f}%</b> · Live price: <code>{live_price}</code>{resumed}"
        )
    return "\n\n".join(lines)


def _telegram_plain_text(html_message: str) -> str:
    """Convert our restricted Telegram HTML to a harmless plain-text fallback."""
    return html.unescape(re.sub(r"</?(?:b|code|i)>", "", html_message))


def send_telegram_message(
    bot_token: str, chat_id: str, text: str, *, request_post: Callable[..., Any] = requests.post,
) -> None:
    if not bot_token or not chat_id:
        raise MonitorConfigurationError("Telegram Bot Token and Chat ID are required for alerts.")
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        response = request_post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
            timeout=max(3, int(os.environ.get("TELEGRAM_REQUEST_TIMEOUT_SECONDS", "15"))),
        )
    except requests.RequestException as exc:
        raise Trading212APIError(f"Telegram delivery failed: {type(exc).__name__}") from exc
    if response.status_code == 400:
        # All monitor-generated content is escaped, but retain a safe delivery
        # fallback if Telegram rejects HTML from a future message format.
        try:
            response = request_post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": _telegram_plain_text(text), "disable_web_page_preview": True},
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
    sample = _telegram_message([{
        "ticker": "SAMPLE", "current_price": 123.45, "currency": "USD",
        "window_minutes": 5, "change_pct": 2.25, "threshold_pct": 2.0, "direction": "up",
    }], _now())
    send_telegram_message(secrets["telegram_bot_token"], secrets["telegram_chat_id"], sample, request_post=request_post)


def is_monitoring_window(now: dt.datetime | None = None) -> bool:
    """Run Trading 212's 5x24 window: Monday 02:00 through Saturday 02:00 Berlin."""
    local = (now or _now()).astimezone(BERLIN_TZ)
    weekday = local.weekday()
    seconds = local.hour * 3_600 + local.minute * 60 + local.second
    if weekday == 0:
        return seconds >= 2 * 3_600
    if 1 <= weekday <= 4:
        return True
    return weekday == 5 and seconds < 2 * 3_600


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
    history_fetcher: Callable[[str, dt.date], list[tuple[Any, ...]]] = _download_yfinance_daily_history,
    volume_fetcher: Callable[[str], float | None] = _download_yfinance_current_volume,
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
        for position in positions:
            _record_observed_position(conn, int(user_id), position, current_time)
        policy = normalize_alert_policy(json.loads(row["alert_policy_json"] or "{}"))
        technical_enabled = any(
            bool(policy["technical"].get(key))
            for key in ("ema_crosses", "macd_crosses", "rsi_levels", "volume_surge")
        )
        # yfinance is intentionally called outside the alert transaction. It
        # is a once-daily completed-bar cache refresh, not a minute-by-minute
        # dependency; movement alerts keep working if a symbol cannot map.
        histories: dict[str, list[float]] = {}
        yfinance_tickers: dict[str, str] = {}
        intraday_volumes: dict[str, tuple[float | None, float | None, str | None]] = {}
        technical_unavailable: dict[str, str] = {}
        if technical_enabled:
            for position in positions:
                yf_ticker, closes, error = _ensure_daily_history(
                    conn, position, current_time, history_fetcher=history_fetcher,
                )
                if closes:
                    histories[position["position_key"]] = closes
                if yf_ticker:
                    yfinance_tickers[position["position_key"]] = yf_ticker
                if error:
                    technical_unavailable[position["ticker"]] = error
            if policy["technical"].get("volume_surge"):
                for position in positions:
                    yf_ticker = yfinance_tickers.get(position["position_key"])
                    if not yf_ticker:
                        continue
                    volume, fraction, trade_date, error = _current_intraday_volume(
                        conn, position, yf_ticker, current_time, volume_fetcher=volume_fetcher,
                    )
                    intraday_volumes[position["position_key"]] = (volume, fraction, trade_date)
                    if error:
                        technical_unavailable.setdefault(position["ticker"], error)
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
                reporting_position = _position_with_report_ticker(
                    conn,
                    position,
                    yfinance_ticker=yfinance_tickers.get(position["position_key"]),
                )
                events.extend(_evaluate_alerts(conn, int(user_id), reporting_position, current_time, policy))
                daily_closes = histories.get(position["position_key"])
                if daily_closes:
                    events.extend(
                        _evaluate_technical_alerts(
                            conn, int(user_id), reporting_position, daily_closes, current_time, policy,
                        )
                    )
                if policy["technical"].get("volume_surge"):
                    volume, fraction, trade_date = intraday_volumes.get(
                        position["position_key"], (None, None, None),
                    )
                    yf_ticker = yfinance_tickers.get(position["position_key"])
                    if yf_ticker:
                        volume_event = _evaluate_volume_surge(
                            conn, int(user_id), reporting_position, yfinance_ticker=yf_ticker,
                            current_volume=volume, session_fraction=fraction,
                            trade_date=trade_date, policy=policy, now=current_time,
                        )
                        if volume_event:
                            events.append(volume_event)
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
        return {
            "status": "ok", "positions": len(positions), "events": events,
            "technical_unavailable": technical_unavailable,
        }
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
