"""User accounts and editable watch-list storage for the multi-user Streamlit app."""

import argparse
import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3

from ticker_mapping import normalize_yfinance_ticker


USER_DB_PATH = os.path.join(os.path.dirname(__file__), "watchlist_users.db")

DEFAULT_STOCK_PAGES = [
    {
        "name": "Core Watchlist",
        "groups": {
            "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
            "Chips/AI": ["TSM", "MU", "ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
            "Fin/Crypto": ["V", "JPM", "BRK-B", "COIN", "HOOD", "MSTR", "CRCL", "SOFI", "OSCR"],
            "Health": ["LLY", "NVO", "ABBV", "UNH"],
            "Energy": ["SMR", "VST", "OKLO", "NEE", "ENPH", "GE", "GEV"],
            "Defense": ["LMT", "BA", "ACHR", "AXON"],
            "Consumer": ["LULU", "NKE", "CMG", "COST"],
            "China": ["BYDDY", "XIACY", "PDD", "BABA", "TCEHY", "BIDU"],
            "Themes": ["ASTS", "CRWV", "NBIS", "MP", "RKLB"],
        },
    }
]

DEFAULT_BROAD_PAGES = [
    {
        "name": "Macro Dashboard",
        "groups": {
            "Dashboard": ["^GSPC", "^NDX", "SPY","QQQ","RSP", "QQQE", "^TNX", "EURUSD=X", "^VIX", "GC=F", "BZ=F", "BTC-USD", "510300.SS"],
            "US Mkt Dir": ["^GSPC", "^NDX", "^DJI", "^RUT"],
            "Breadth": ["RSP", "QQQE"],
            "AI/Tech Risk": ["TQQQ", "^SOX"],
            "China Beta": ["510300.SS", "510050.SS", "159915.SZ", "588000.SS", "3033.HK"],
            "Rates/FX": ["^TNX", "EURUSD=X", "EURCNY=X"],
            "Fear/Vol": ["^VIX", "^VXN"],
            "Safe Haven": ["GC=F", "SI=F"],
            "Oil/Geopol": ["BZ=F"],
            "Crypto": ["BTC-USD", "ETH-USD"],
            "Strat Resources": ["WNUC.DE", "REMX"],
        },
    }
]

BREADTH_GROUPS = {
    "S&P 500 Breadth": ["SP500_20MA_Ratio", "SP500_50MA_Ratio", "SP500_200MA_Ratio"],
    "Nasdaq 100 Breadth": ["NDX100_20MA_Ratio", "NDX100_50MA_Ratio", "NDX100_200MA_Ratio"],
}


def default_watchlist_config():
    return normalize_config({
        "stocks_pages": DEFAULT_STOCK_PAGES,
        "broad_pages": DEFAULT_BROAD_PAGES,
    })


def normalize_config(config):
    return {
        "stocks_pages": _normalize_pages(config.get("stocks_pages") or DEFAULT_STOCK_PAGES, "Core Watchlist"),
        "broad_pages": _normalize_pages(config.get("broad_pages") or DEFAULT_BROAD_PAGES, "Macro Dashboard"),
    }


def _normalize_pages(pages, fallback_name):
    normalized = []
    for i, page in enumerate(pages or []):
        name = str(page.get("name") or f"{fallback_name} {i + 1}").strip()
        groups = {}
        raw_groups = page.get("groups") or {}
        for group_name, tickers in raw_groups.items():
            group_name = str(group_name or "Group").strip()
            normalized_tickers = [
                normalize_yfinance_ticker(t)
                for t in tickers
                if normalize_yfinance_ticker(t)
            ]
            groups[group_name] = list(dict.fromkeys(normalized_tickers))
        normalized.append({"name": name, "groups": groups})
    return normalized or [{"name": fallback_name, "groups": {}}]


def init_user_db():
    conn = sqlite3.connect(USER_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_configs (
            user_id INTEGER PRIMARY KEY,
            config_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            username TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            first_failure_at TEXT,
            last_failure_at TEXT,
            locked_until TEXT
        )
    """)
    conn.commit()
    return conn


def make_password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000${}${}".format(
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password, stored_hash):
    try:
        algo, iterations, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# ── Login brute-force protection ──────────────────────────────
# A dummy hash used to keep response timing consistent when the
# requested username does not exist (prevents timing-based user
# enumeration).
_DUMMY_HASH = "pbkdf2_sha256$200000$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAA=="


def _get_login_config():
    """Return rate-limiting config from env vars.

    Minimums are enforced so that core protection cannot be fully
    disabled:

    * max_failures >= 3
    * lockout_seconds >= 60
    * window_seconds >= 60
    """
    try:
        max_failures = int(os.environ.get("LOGIN_MAX_FAILURES", "5"))
    except (TypeError, ValueError):
        max_failures = 5
    max_failures = max(max_failures, 3)

    try:
        lockout_seconds = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900"))
    except (TypeError, ValueError):
        lockout_seconds = 900
    lockout_seconds = max(lockout_seconds, 60)

    try:
        window_seconds = int(os.environ.get("LOGIN_WINDOW_SECONDS", "1800"))
    except (TypeError, ValueError):
        window_seconds = 1800
    window_seconds = max(window_seconds, 60)

    return {
        "max_failures": max_failures,
        "lockout_seconds": lockout_seconds,
        "window_seconds": window_seconds,
    }


def check_login_lock_status(username):
    """Check if *username* is currently locked due to failed attempts.

    Returns ``(is_locked, remaining_seconds)``.

    This function does **not** reveal whether the user account actually
    exists — it only inspects the ``login_attempts`` table which tracks
    all attempted usernames.
    """
    username = str(username).strip()
    if not username:
        return (False, 0)
    config = _get_login_config()
    conn = init_user_db()
    try:
        row = conn.execute(
            "SELECT failed_count, last_failure_at, locked_until "
            "FROM login_attempts WHERE username=?",
            (username,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return (False, 0)

    _failed_count, last_failure_at, locked_until = row
    now = _dt.datetime.now()

    if locked_until:
        try:
            locked_until_dt = _dt.datetime.fromisoformat(locked_until)
        except (ValueError, TypeError):
            locked_until_dt = None
        if locked_until_dt and now < locked_until_dt:
            remaining = int((locked_until_dt - now).total_seconds())
            return (True, max(remaining, 0))

    # If the observation window has expired, the count is effectively
    # reset — caller should treat this as not-locked.
    if last_failure_at:
        try:
            last_dt = _dt.datetime.fromisoformat(last_failure_at)
            if (now - last_dt).total_seconds() > config["window_seconds"]:
                return (False, 0)
        except (ValueError, TypeError):
            pass

    return (False, 0)


def user_cache_key(username):
    key = re.sub(r"[^A-Za-z0-9_-]+", "_", str(username).strip().lower())
    return key[:64].strip("_")


def create_user(username, password, display_name=None, overwrite=False):
    username = str(username).strip()
    if not username:
        raise ValueError("username is required")
    password_hash = make_password_hash(password)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    conn = init_user_db()
    if overwrite:
        conn.execute(
            "INSERT OR REPLACE INTO users (id, username, password_hash, display_name, is_active, created_at) "
            "VALUES ((SELECT id FROM users WHERE username=?), ?, ?, ?, 1, COALESCE((SELECT created_at FROM users WHERE username=?), ?))",
            (username, username, password_hash, display_name or username, username, now),
        )
    else:
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            (username, password_hash, display_name or username, now),
        )
    user_id = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
    row = conn.execute("SELECT user_id FROM watchlist_configs WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        save_user_config_by_id(conn, user_id, default_watchlist_config())
    conn.commit()
    conn.close()


def authenticate(username, password):
    """Verify credentials and apply brute-force protection.

    Returns the user dict on success, ``None`` on any failure (wrong
    password, locked, or non-existent user — all identical to the
    caller).

    Concurrency: the entire check-and-update cycle runs inside a
    ``BEGIN IMMEDIATE`` transaction so that concurrent login attempts
    cannot lose failure counts or bypass the lock threshold.
    """
    username = str(username).strip()
    config = _get_login_config()
    conn = init_user_db()
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")

        # ── Check current lock status ──
        attempt_row = conn.execute(
            "SELECT failed_count, last_failure_at, locked_until "
            "FROM login_attempts WHERE username=?",
            (username,),
        ).fetchone()

        now = _dt.datetime.now()
        now_iso = now.isoformat(timespec="seconds")

        if attempt_row:
            failed_count, last_failure_at, locked_until = attempt_row
            # Check if currently locked
            if locked_until:
                try:
                    locked_until_dt = _dt.datetime.fromisoformat(locked_until)
                except (ValueError, TypeError):
                    locked_until_dt = None
                if locked_until_dt and now < locked_until_dt:
                    conn.execute("COMMIT")
                    return None  # locked — same as any failure

            # Check if window has expired — reset count
            if last_failure_at:
                try:
                    last_dt = _dt.datetime.fromisoformat(last_failure_at)
                    if (now - last_dt).total_seconds() > config["window_seconds"]:
                        failed_count = 0
                except (ValueError, TypeError):
                    pass
        else:
            failed_count = 0

        # ── Query user and verify password ──
        user_row = conn.execute(
            "SELECT id, username, password_hash, display_name "
            "FROM users WHERE username=? AND is_active=1",
            (username,),
        ).fetchone()

        if user_row:
            password_ok = verify_password(password, user_row[2])
        else:
            # Dummy verify to keep timing consistent (prevents
            # timing-based user enumeration).
            verify_password(password, _DUMMY_HASH)
            password_ok = False

        if password_ok:
            # ── Success: clear all failure records ──
            conn.execute(
                "DELETE FROM login_attempts WHERE username=?", (username,)
            )
            conn.execute("COMMIT")
            return {
                "id": user_row[0],
                "username": user_row[1],
                "display_name": user_row[3] or user_row[1],
                "cache_key": user_cache_key(user_row[1]),
            }
        else:
            # ── Failure: increment count, maybe lock ──
            new_count = failed_count + 1
            if new_count >= config["max_failures"]:
                locked_until_iso = (
                    now + _dt.timedelta(seconds=config["lockout_seconds"])
                ).isoformat(timespec="seconds")
            else:
                locked_until_iso = None

            if attempt_row:
                conn.execute(
                    "UPDATE login_attempts "
                    "SET failed_count=?, last_failure_at=?, locked_until=? "
                    "WHERE username=?",
                    (new_count, now_iso, locked_until_iso, username),
                )
            else:
                conn.execute(
                    "INSERT INTO login_attempts "
                    "(username, failed_count, first_failure_at, last_failure_at, locked_until) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (username, new_count, now_iso, now_iso, locked_until_iso),
                )
            conn.execute("COMMIT")
            return None
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def get_user_config(user_id):
    conn = init_user_db()
    row = conn.execute("SELECT config_json FROM watchlist_configs WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        config = default_watchlist_config()
        save_user_config_by_id(conn, user_id, config)
        conn.commit()
    else:
        config = normalize_config(json.loads(row[0]))
    conn.close()
    return config


def save_user_config(user_id, config):
    conn = init_user_db()
    save_user_config_by_id(conn, user_id, config)
    conn.commit()
    conn.close()


def save_user_config_by_id(conn, user_id, config):
    normalized = normalize_config(config)
    conn.execute(
        "INSERT OR REPLACE INTO watchlist_configs (user_id, config_json, updated_at) VALUES (?, ?, ?)",
        (user_id, json.dumps(normalized, ensure_ascii=False), _dt.datetime.now().isoformat(timespec="seconds")),
    )


def config_to_api_groups(config):
    groups = {}
    for section, prefix in (("stocks_pages", "S"), ("broad_pages", "B")):
        for page in config.get(section, []):
            for group_name, tickers in page.get("groups", {}).items():
                groups[f"{prefix}:{page['name']}:{group_name}"] = tickers
    return groups


def broad_market_tickers(config):
    tickers = []
    for page in config.get("broad_pages", []):
        for group_tickers in page.get("groups", {}).values():
            tickers.extend(group_tickers)
    return list(dict.fromkeys(tickers))


def _main():
    import getpass
    import sys as _sys

    parser = argparse.ArgumentParser(description="Manage Stock Watch List users")
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create-user")
    create.add_argument("username")
    create.add_argument(
        "--password",
        default=None,
        help=(
            "(deprecated, insecure) password — will appear in shell "
            "history and process listings. Omit for a secure prompt."
        ),
    )
    create.add_argument("--display-name")
    create.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.cmd == "create-user":
        password = args.password
        if password is None:
            password = getpass.getpass(f"Password for {args.username}: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                raise SystemExit("Passwords do not match.")
        else:
            print(
                "Warning: --password is insecure and will appear in shell "
                "history and process listings. Omit it for a secure prompt.",
                file=_sys.stderr,
            )
        if not password:
            raise SystemExit("Password cannot be empty.")
        try:
            create_user(
                args.username, password, args.display_name, overwrite=args.overwrite
            )
        except sqlite3.IntegrityError:
            raise SystemExit(
                f"User already exists: {args.username}. "
                "Use --overwrite to reset this user's password."
            )
        print(f"User ready: {args.username}")


if __name__ == "__main__":
    _main()
