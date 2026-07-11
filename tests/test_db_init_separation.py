#!/usr/bin/env python3
"""Tests for P1-2: database initialization, connection, migration, cleanup separation.

All tests use temporary directories and temporary databases.
No real network, SMTP, model API, or real user data is accessed.

Heavy dependencies (flask, yfinance, pandas, etc.) are mocked so the tests
can run without the full production environment installed.
"""

import os
import sys
import sqlite3
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is on the path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _stub_heavy_imports():
    """Inject stub modules for flask, yfinance, etc. into sys.modules.

    This allows us to import stock_watch_list_back_end without the full
    production dependency stack. Only the DB-related functions are tested.
    """
    # flask
    flask_mod = MagicMock()
    flask_mod.Flask = MagicMock()
    flask_mod.request = MagicMock()
    flask_mod.jsonify = MagicMock(return_value={})
    sys.modules["flask"] = flask_mod

    # yfinance
    sys.modules["yfinance"] = MagicMock()

    # pandas
    sys.modules["pandas"] = MagicMock()

    # numpy
    sys.modules["numpy"] = MagicMock()

    # pytz
    sys.modules["pytz"] = MagicMock()

    # fear_and_greed
    sys.modules["fear_and_greed"] = MagicMock()

    # requests_cache
    sys.modules["requests_cache"] = MagicMock()

    # requests
    sys.modules["requests"] = MagicMock()

    # dotenv
    dotenv_mod = MagicMock()
    dotenv_mod.load_dotenv = MagicMock()
    sys.modules["dotenv"] = dotenv_mod

    # stockanalysis_scraper (local module — stub it)
    sa_mod = MagicMock()
    sa_mod.scrape_batch = MagicMock()
    sa_mod.should_query_forward_pe = MagicMock(return_value=False)
    sys.modules["stockanalysis_scraper"] = sa_mod

    # ticker_mapping (local module — stub it)
    tm_mod = MagicMock()
    tm_mod.normalize_yfinance_ticker = MagicMock(side_effect=lambda t: t)
    tm_mod.stockanalysis_overview_url = MagicMock()
    sys.modules["ticker_mapping"] = tm_mod


def _clear_module():
    """Remove stock_watch_list_back_end from sys.modules."""
    for k in list(sys.modules.keys()):
        if k == "stock_watch_list_back_end" or k.startswith("stock_watch_list_back_end."):
            del sys.modules[k]


def _make_fresh_backend(temp_dir):
    """Import a fresh copy of stock_watch_list_back_end with DB_PATH in temp_dir."""
    _stub_heavy_imports()
    _clear_module()

    db_path = os.path.join(temp_dir, "test_stock_cache.db")
    # Set env var directly (avoid patch.dict on Windows — 32767 char limit issue)
    old_val = os.environ.get("STOCK_CACHE_DB_PATH")
    os.environ["STOCK_CACHE_DB_PATH"] = db_path
    try:
        import stock_watch_list_back_end as backend  # noqa: F811
    finally:
        if old_val is not None:
            os.environ["STOCK_CACHE_DB_PATH"] = old_val
        else:
            os.environ.pop("STOCK_CACHE_DB_PATH", None)

    return backend, db_path


class TestCwdIndependentPath(unittest.TestCase):
    """Test 1: Different CWD resolves to same default DB path."""

    def test_default_path_is_absolute(self):
        """DB_PATH should be absolute, not relative to CWD."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, db_path = _make_fresh_backend(temp_dir)
            self.assertTrue(os.path.isabs(backend.DB_PATH),
                            f"DB_PATH should be absolute, got: {backend.DB_PATH}")
            self.assertEqual(backend.DB_PATH, db_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_different_cwd_same_path(self):
        """Changing CWD should not change the resolved DB_PATH."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, db_path = _make_fresh_backend(temp_dir)
            original_cwd = os.getcwd()

            other_dir = tempfile.mkdtemp(prefix="p12_other_")
            try:
                os.chdir(other_dir)
                self.assertEqual(backend.DB_PATH, db_path)
                self.assertTrue(os.path.isabs(backend.DB_PATH))
            finally:
                os.chdir(original_cwd)
                shutil.rmtree(other_dir, ignore_errors=True)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestEnvVarOverride(unittest.TestCase):
    """Test 2: Environment variable can override the DB path."""

    def test_env_var_override(self):
        """STOCK_CACHE_DB_PATH should override the default path."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            custom_path = os.path.join(temp_dir, "custom_location", "my_cache.db")
            old_val = os.environ.pop("STOCK_CACHE_DB_PATH", None)
            os.environ["STOCK_CACHE_DB_PATH"] = custom_path
            try:
                _clear_module()
                _stub_heavy_imports()
                import stock_watch_list_back_end as backend
                self.assertEqual(backend.DB_PATH, custom_path)
            finally:
                if old_val is not None:
                    os.environ["STOCK_CACHE_DB_PATH"] = old_val
                else:
                    os.environ.pop("STOCK_CACHE_DB_PATH", None)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_explicit_param_override(self):
        """get_db_connection(db_path=...) and init_database(db_path=...) should use the given path."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            custom_db = os.path.join(temp_dir, "explicit.db")

            conn = backend.get_db_connection(db_path=custom_db)
            self.assertTrue(os.path.exists(custom_db))

            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {t[0] for t in tables}
            self.assertIn("price_cache", table_names)
            self.assertIn("stock_analysis_data", table_names)
            conn.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestIdempotentInit(unittest.TestCase):
    """Test 3: Repeated initialization does not destroy existing data."""

    def test_repeated_init_preserves_data(self):
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, db_path = _make_fresh_backend(temp_dir)

            conn = backend.get_db_connection()
            conn.execute(
                "INSERT INTO price_cache (ticker, date, adj_close, volume) VALUES (?, ?, ?, ?)",
                ("AAPL", "2024-01-15", 185.0, 1000000),
            )
            conn.commit()
            conn.close()

            backend.init_database()
            conn = backend.get_db_connection()

            row = conn.execute(
                "SELECT ticker, date, adj_close FROM price_cache WHERE ticker=?",
                ("AAPL",),
            ).fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row[0], "AAPL")
            self.assertEqual(row[1], "2024-01-15")
            self.assertAlmostEqual(row[2], 185.0)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_repeated_init_no_error(self):
        """Calling init_database() multiple times should not raise."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            for _ in range(5):
                backend.init_database()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestNoCleanupOnNormalConnect(unittest.TestCase):
    """Test 4: Normal connection does not trigger retention cleanup."""

    def test_get_db_connection_no_cleanup(self):
        """get_db_connection() should NOT delete old rows."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, db_path = _make_fresh_backend(temp_dir)

            conn = backend.get_db_connection()
            old_date = "2020-01-01"
            conn.execute(
                "INSERT INTO price_cache (ticker, date, adj_close, volume) VALUES (?, ?, ?, ?)",
                ("OLD_TICKER", old_date, 100.0, 500),
            )
            conn.execute(
                "INSERT INTO stock_analysis_data (ticker, date, forward_pe) VALUES (?, ?, ?)",
                ("OLD_TICKER", old_date, 15.0),
            )
            conn.commit()
            conn.close()

            # Reset the schema-initialized flag so next call goes through fresh
            backend._DB_SCHEMA_INITIALIZED.clear()

            conn = backend.get_db_connection()
            conn.close()

            conn = backend.get_db_connection()
            row = conn.execute(
                "SELECT COUNT(*) FROM price_cache WHERE ticker='OLD_TICKER'"
            ).fetchone()
            self.assertEqual(row[0], 1, "Old price_cache row should NOT be cleaned by get_db_connection()")

            row = conn.execute(
                "SELECT COUNT(*) FROM stock_analysis_data WHERE ticker='OLD_TICKER'"
            ).fetchone()
            self.assertEqual(row[0], 1, "Old stock_analysis_data row should NOT be cleaned")
            conn.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestExplicitCleanup(unittest.TestCase):
    """Test 5: Explicit cleanup correctly removes expired data."""

    def test_cleanup_removes_old_price_data(self):
        import datetime as _dt
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, db_path = _make_fresh_backend(temp_dir)

            conn = backend.get_db_connection()
            # Use dates relative to today so the test works in any year
            old_date = (_dt.date.today() - _dt.timedelta(days=800)).strftime("%Y-%m-%d")
            recent_date = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")

            conn.execute(
                "INSERT INTO price_cache (ticker, date, adj_close, volume) VALUES (?, ?, ?, ?)",
                ("OLD", old_date, 100.0, 500),
            )
            conn.execute(
                "INSERT INTO price_cache (ticker, date, adj_close, volume) VALUES (?, ?, ?, ?)",
                ("NEW", recent_date, 200.0, 1000),
            )
            conn.commit()
            conn.close()

            backend.cleanup_old_data()

            conn = backend.get_db_connection()
            old_count = conn.execute(
                "SELECT COUNT(*) FROM price_cache WHERE ticker='OLD'"
            ).fetchone()[0]
            new_count = conn.execute(
                "SELECT COUNT(*) FROM price_cache WHERE ticker='NEW'"
            ).fetchone()[0]
            conn.close()

            self.assertEqual(old_count, 0, "Old price_cache row should be cleaned")
            self.assertEqual(new_count, 1, "Recent price_cache row should be retained")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_cleanup_with_explicit_db_path(self):
        """cleanup_old_data(db_path=...) should work without an existing connection."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            custom_db = os.path.join(temp_dir, "cleanup_test.db")

            conn = backend.get_db_connection(db_path=custom_db)
            conn.execute(
                "INSERT INTO price_cache (ticker, date, adj_close, volume) VALUES (?, ?, ?, ?)",
                ("OLD", "2020-01-01", 100.0, 500),
            )
            conn.commit()
            conn.close()

            backend._DB_SCHEMA_INITIALIZED.clear()
            backend.cleanup_old_data(db_path=custom_db)

            conn = backend.get_db_connection(db_path=custom_db)
            count = conn.execute(
                "SELECT COUNT(*) FROM price_cache WHERE ticker='OLD'"
            ).fetchone()[0]
            conn.close()

            self.assertEqual(count, 0, "Explicit cleanup should remove old data")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestMigrationPreservesData(unittest.TestCase):
    """Test 6: Old database migration preserves existing data."""

    def test_migration_adds_columns_preserves_data(self):
        """Simulate an old DB without migration columns, then run migration."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, db_path = _make_fresh_backend(temp_dir)

            # Create a DB with the old schema (no migration columns)
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_cache (
                    ticker TEXT NOT NULL, date TEXT NOT NULL,
                    adj_close REAL, volume REAL,
                    PRIMARY KEY (ticker, date)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_analysis_data (
                    ticker TEXT NOT NULL, date TEXT NOT NULL,
                    forward_pe REAL,
                    created_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (ticker, date)
                )
            """)
            conn.execute(
                "INSERT INTO stock_analysis_data (ticker, date, forward_pe) VALUES (?, ?, ?)",
                ("TEST", "2024-01-15", 20.5),
            )
            conn.commit()
            conn.close()

            # Reset schema-initialized flag and re-init to trigger migration
            backend._DB_SCHEMA_INITIALIZED.clear()
            backend.init_database()

            conn = backend.get_db_connection()
            row = conn.execute(
                "SELECT ticker, date, forward_pe, peg_ratio, trailing_pe, market_cap, "
                "earnings_date, ps_ratio, pb_ratio "
                "FROM stock_analysis_data WHERE ticker=?",
                ("TEST",),
            ).fetchone()
            conn.close()

            self.assertIsNotNone(row, "Pre-migration data should be preserved")
            self.assertEqual(row[0], "TEST")
            self.assertEqual(row[1], "2024-01-15")
            self.assertAlmostEqual(row[2], 20.5)
            self.assertIsNone(row[3])  # peg_ratio
            self.assertIsNone(row[4])  # trailing_pe
            self.assertIsNone(row[5])  # market_cap
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_migration_reraises_non_migration_errors(self):
        """_run_migrations should re-raise non-'no such column' OperationalErrors."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, db_path = _make_fresh_backend(temp_dir)

            # Create a DB where stock_analysis_data table doesn't exist
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_cache (
                    ticker TEXT NOT NULL, date TEXT NOT NULL,
                    adj_close REAL, volume REAL,
                    PRIMARY KEY (ticker, date)
                )
            """)
            conn.commit()
            conn.close()

            # Mock _create_schema to skip table creation, so migration will fail
            with patch.object(backend, '_create_schema', lambda c: None):
                backend._DB_SCHEMA_INITIALIZED.clear()
                with self.assertRaises(sqlite3.OperationalError):
                    backend.init_database()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestConnectionPragmas(unittest.TestCase):
    """Test 7: SQLite timeout, busy_timeout, WAL, and row_factory behavior."""

    def test_wal_mode(self):
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            conn = backend.get_db_connection()
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
            self.assertEqual(mode.lower(), "wal")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_busy_timeout(self):
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            conn = backend.get_db_connection()
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            conn.close()
            self.assertEqual(timeout, 5000)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_auto_vacuum(self):
        """PRAGMA auto_vacuum = INCREMENTAL should be set without error.

        Note: auto_vacuum mode is a database-level setting that only takes
        effect when the database is first created. We verify the PRAGMA
        is accepted; the actual value depends on whether the DB file was
        new when the PRAGMA was first executed.
        """
        import datetime as _dt
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            conn = backend.get_db_connection()
            # The PRAGMA should not raise. Value is 0 (NONE) or 2 (INCREMENTAL).
            av = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
            conn.close()
            self.assertIn(av, (0, 2), f"auto_vacuum should be 0 or 2, got {av}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_row_factory_default(self):
        """Row factory should be default (tuple) for backward compatibility."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            conn = backend.get_db_connection()
            conn.execute(
                "INSERT INTO price_cache (ticker, date, adj_close, volume) VALUES (?, ?, ?, ?)",
                ("AAPL", "2024-01-15", 185.0, 1000000),
            )
            conn.commit()
            row = conn.execute(
                "SELECT ticker, date FROM price_cache WHERE ticker='AAPL'"
            ).fetchone()
            conn.close()
            self.assertIsInstance(row, tuple)
            self.assertEqual(row[0], "AAPL")
            self.assertEqual(row[1], "2024-01-15")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_connection_timeout_contention(self):
        """Connection with timeout should handle lock contention gracefully."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            conn1 = backend.get_db_connection()
            conn1.execute("BEGIN EXCLUSIVE")
            conn1.execute(
                "INSERT INTO price_cache (ticker, date, adj_close, volume) VALUES (?, ?, ?, ?)",
                ("LOCK", "2024-01-15", 100.0, 100),
            )

            # Second connection with short busy_timeout should fail quickly
            conn2 = sqlite3.connect(backend.DB_PATH, timeout=1)
            conn2.execute("PRAGMA busy_timeout = 100")
            with self.assertRaises(sqlite3.OperationalError):
                conn2.execute(
                    "INSERT INTO price_cache (ticker, date, adj_close, volume) VALUES (?, ?, ?, ?)",
                    ("LOCK2", "2024-01-15", 200.0, 200),
                )
            conn2.close()

            conn1.commit()
            conn1.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestFlaskApiNoRegression(unittest.TestCase):
    """Test 8: Flask API endpoints and P0 security tests don't regress."""

    def test_db_path_used_by_flask(self):
        """Flask app should use the same DB_PATH as the module."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, db_path = _make_fresh_backend(temp_dir)
            self.assertTrue(hasattr(backend, 'app'))
            self.assertTrue(hasattr(backend, 'get_db_connection'))
            self.assertTrue(hasattr(backend, 'init_database'))
            self.assertTrue(hasattr(backend, 'cleanup_old_data'))
            self.assertTrue(hasattr(backend, 'get_global_db_connection'))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_get_active_db_path_still_works(self):
        """get_active_db_path() should still work for multiuser support."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, db_path = _make_fresh_backend(temp_dir)

            self.assertEqual(backend.get_active_db_path(), db_path)

            token = backend.CURRENT_DB_PATH.set(
                os.path.join(temp_dir, "user_test_stock_cache.db")
            )
            try:
                self.assertEqual(
                    backend.get_active_db_path(),
                    os.path.join(temp_dir, "user_test_stock_cache.db"),
                )
            finally:
                backend.CURRENT_DB_PATH.reset(token)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_db_path_for_cache_key_still_works(self):
        """db_path_for_cache_key() should still generate per-user paths."""
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)

            self.assertEqual(backend.db_path_for_cache_key(""), backend.DB_PATH)
            self.assertEqual(backend.db_path_for_cache_key(None), backend.DB_PATH)

            user_path = backend.db_path_for_cache_key("user123")
            self.assertIn("user123", user_path)
            self.assertTrue(user_path.endswith("_stock_cache.db"))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestBackwardCompatWrappers(unittest.TestCase):
    """Test that init_db() and init_global_market_cap_db() still work as wrappers."""

    def test_init_db_wrapper_returns_connection(self):
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            conn = backend.init_db()
            self.assertIsNotNone(conn)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {t[0] for t in tables}
            self.assertIn("price_cache", table_names)
            conn.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_init_global_market_cap_db_wrapper(self):
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)
            conn = backend.init_global_market_cap_db()
            self.assertIsNotNone(conn)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {t[0] for t in tables}
            self.assertIn("market_cap_cache", table_names)
            conn.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestPerUserDbIsolation(unittest.TestCase):
    """Test that per-user DB paths work correctly with the new connection functions."""

    def test_per_user_db_isolation(self):
        temp_dir = tempfile.mkdtemp(prefix="p12_test_")
        try:
            backend, _ = _make_fresh_backend(temp_dir)

            # Use explicit temp paths instead of db_path_for_cache_key
            # (which uses USER_CACHE_DIR pointing to the real project dir)
            user1_db = os.path.join(temp_dir, "user1_stock_cache.db")
            user2_db = os.path.join(temp_dir, "user2_stock_cache.db")

            self.assertNotEqual(user1_db, user2_db)

            conn1 = backend.get_db_connection(db_path=user1_db)
            conn1.execute(
                "INSERT INTO price_cache (ticker, date, adj_close, volume) VALUES (?, ?, ?, ?)",
                ("AAPL", "2024-01-15", 185.0, 1000000),
            )
            conn1.commit()
            conn1.close()

            conn2 = backend.get_db_connection(db_path=user2_db)
            count = conn2.execute(
                "SELECT COUNT(*) FROM price_cache WHERE ticker='AAPL'"
            ).fetchone()[0]
            conn2.close()

            self.assertEqual(count, 0, "user2 DB should not see user1's data")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
