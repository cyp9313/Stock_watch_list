"""Shared market data service for the Stock Watchlist + AI Daily Report project.

This module provides a unified provider layer for market data that can be called
independently by CLI scripts, the report worker, and the Flask backend without
requiring any of those services to be running.

Design goals (P1-1B):
- Single source of truth for OHLCV downloads within one report generation.
- Unified ticker mapping, currency, and error structure.
- Clear distinction between actual zero, missing data, not-applicable, and
  provider failure via ``DataStatus``.
- No Flask dependency — importable from any Python process.
- Backward-compatible: existing scripts keep their CLI interface and output format.

Usage by the daily report pipeline::

    from market_data_service import MarketDataService

    # fetch_and_calc.py
    raw = MarketDataService.fetch_ohlcv(TICKER, period="1y", auto_adjust=False)
    MarketDataService.save_ohlcv_snapshot(raw, TICKER, run_dir=".")
    info = MarketDataService.fetch_ticker_info(TICKER)
    sa  = MarketDataService.fetch_stock_analysis(TICKER)

    # gen_chart.py (runs later in the same run_dir)
    data = MarketDataService.load_ohlcv_snapshot(TICKER, run_dir=".")
    if data is None:
        data = MarketDataService.fetch_ohlcv(TICKER, period="1y", auto_adjust=False)
"""

from __future__ import annotations

import os
import pickle
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Data status enum
# ---------------------------------------------------------------------------

class DataStatus(Enum):
    """Distinguish four semantically different data states.

    - ``ACTUAL``: The provider returned a real value, including zero.
    - ``MISSING``: The provider returned no value for this field.
    - ``NOT_APPLICABLE``: The field does not apply to this instrument
      (e.g. P/E ratio for an index).
    - ``PROVIDER_ERROR``: The provider failed (network error, parse error, etc.).
    """

    ACTUAL = "actual"
    MISSING = "missing"
    NOT_APPLICABLE = "n/a"
    PROVIDER_ERROR = "error"


# ---------------------------------------------------------------------------
# Market data service
# ---------------------------------------------------------------------------

class MarketDataService:
    """Shared market data service — callable by CLI, worker, and Flask independently.

    All methods are static so the service can be used without instantiation.
    An optional ``cache_db_path`` can be set via environment variable
    ``MARKET_DATA_CACHE_DB`` for future SQLite caching, but the current
    implementation relies on file-based OHLCV snapshots for same-report
    data sharing.
    """

    # -- OHLCV ---------------------------------------------------------------

    @staticmethod
    def fetch_ohlcv(
        ticker: str,
        period: str = "1y",
        interval: str = "1d",
        auto_adjust: bool = False,
    ) -> pd.DataFrame:
        """Fetch OHLCV data via yfinance.

        Args:
            ticker: yfinance-style ticker (caller is responsible for normalization).
            period: yfinance period string ("1y", "2y", "6mo", etc.).
            interval: yfinance interval ("1d", "1h", "4h", etc.).
            auto_adjust: ``False`` preserves the ``Adj Close`` column;
                         ``True`` makes ``Close`` the adjusted price.

        Returns:
            DataFrame with columns: Open, High, Low, Close, Adj Close (if
            auto_adjust=False), Volume.  MultiIndex columns are flattened to
            simple column names.
        """
        data = yf.download(
            ticker,
            period=period,
            interval=interval,
            auto_adjust=auto_adjust,
        )
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = [
                c[0] if isinstance(c, tuple) else c for c in data.columns
            ]
        return data

    # -- Snapshot save / load ------------------------------------------------

    @staticmethod
    def _snapshot_filename(ticker: str) -> str:
        """Return the deterministic snapshot filename for a ticker."""
        safe = ticker.upper().replace("-", "_").replace("^", "IDX_").replace(".", "_")
        return f"{safe}_ohlcv_snapshot.pkl"

    @staticmethod
    def save_ohlcv_snapshot(
        data: pd.DataFrame,
        ticker: str,
        run_dir: str | Path | None = None,
    ) -> Path:
        """Save OHLCV DataFrame to a pickle file for reuse by gen_chart.py.

        The snapshot is written to ``run_dir`` (defaults to current directory).
        Both fetch_and_calc.py and gen_chart.py share the same ``run_dir``
        when invoked by the report pipeline, so gen_chart.py can load this
        snapshot instead of downloading data again.

        Returns the path to the saved snapshot file.
        """
        directory = Path(run_dir) if run_dir else Path(".")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / MarketDataService._snapshot_filename(ticker)
        with open(path, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        return path

    @staticmethod
    def load_ohlcv_snapshot(
        ticker: str,
        run_dir: str | Path | None = None,
    ) -> pd.DataFrame | None:
        """Load a previously saved OHLCV snapshot.

        Returns ``None`` if no snapshot exists (e.g. when gen_chart.py is
        run standalone without a preceding fetch_and_calc.py call).
        """
        directory = Path(run_dir) if run_dir else Path(".")
        path = directory / MarketDataService._snapshot_filename(ticker)
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    @staticmethod
    def clear_ohlcv_snapshot(
        ticker: str,
        run_dir: str | Path | None = None,
    ) -> None:
        """Remove a snapshot file if it exists (cleanup)."""
        directory = Path(run_dir) if run_dir else Path(".")
        path = directory / MarketDataService._snapshot_filename(ticker)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    # -- Ticker info ---------------------------------------------------------

    @staticmethod
    def fetch_ticker_info(ticker: str) -> dict[str, Any]:
        """Fetch yfinance Ticker().info for the given ticker.

        Returns an empty dict on failure (same behavior as the original
        fetch_and_calc.py try/except block).
        """
        try:
            return yf.Ticker(ticker).info or {}
        except Exception:
            return {}

    # -- StockAnalysis fundamentals ------------------------------------------

    @staticmethod
    def fetch_stock_analysis(ticker: str) -> dict[str, Any]:
        """Fetch StockAnalysis.com fundamentals via the merged scraper.

        Returns an empty dict if the scraper is unavailable or the ticker
        is not supported by StockAnalysis.

        The scraper returns 20 fields (V5.8 superset).  Callers that only
        need the original 9 fields can access them by key — the superset
        is backward-compatible.
        """
        try:
            from stockanalysis_scraper import (
                scrape_stock_analysis,
                should_query_forward_pe,
            )
        except ImportError:
            return {}

        if should_query_forward_pe is None or should_query_forward_pe(ticker):
            data = scrape_stock_analysis(ticker)
            return data if data else {}
        return {}

    # -- Ticker normalization ------------------------------------------------

    @staticmethod
    def normalize_ticker(ticker: str) -> str:
        """Delegate to ticker_mapping.normalize_yfinance_ticker()."""
        try:
            from ticker_mapping import normalize_yfinance_ticker
            return normalize_yfinance_ticker(ticker)
        except ImportError:
            return str(ticker or "").strip().upper()

    # -- Data status helpers -------------------------------------------------

    @staticmethod
    def classify_field(
        value: Any,
        *,
        instrument_type: str | None = None,
        not_applicable_for: set[str] | None = None,
    ) -> DataStatus:
        """Classify a data field value into a DataStatus.

        Args:
            value: The field value (float, int, str, or None).
            instrument_type: The instrument type (e.g. "INDEX", "ETF", "CRYPTO").
            not_applicable_for: Instrument types for which this field is N/A.

        Returns:
            - ``ACTUAL`` if value is a real number (including 0).
            - ``NOT_APPLICABLE`` if instrument_type is in not_applicable_for.
            - ``MISSING`` if value is None or empty.
            - ``PROVIDER_ERROR`` if value is a string starting with "request_error".
        """
        if (
            instrument_type
            and not_applicable_for
            and instrument_type in not_applicable_for
        ):
            return DataStatus.NOT_APPLICABLE
        if value is None:
            return DataStatus.MISSING
        if isinstance(value, str):
            if value.startswith("request_error"):
                return DataStatus.PROVIDER_ERROR
            if value.lower() in {"n/a", "na", "-", "\u2014"}:
                return DataStatus.NOT_APPLICABLE
            return DataStatus.MISSING
        if isinstance(value, (int, float)):
            return DataStatus.ACTUAL
        return DataStatus.MISSING


# ---------------------------------------------------------------------------
# Report data snapshot
# ---------------------------------------------------------------------------

class ReportDataSnapshot:
    """Lazily-loaded data snapshot for a single report generation.

    Ensures that fetch_and_calc.py and gen_chart.py use the same OHLCV data
    within one report run.  The first caller (typically fetch_and_calc.py)
    downloads data and saves a snapshot; the second caller (gen_chart.py)
    loads the snapshot instead of re-downloading.

    Usage::

        snapshot = ReportDataSnapshot("AAPL", run_dir=".")
        raw = snapshot.ohlcv  # downloads on first access, caches in memory
        # gen_chart.py can reuse the same snapshot file
    """

    def __init__(
        self,
        ticker: str,
        run_dir: str | Path | None = None,
        period: str = "1y",
        auto_adjust: bool = False,
    ):
        self._ticker = ticker
        self._run_dir = run_dir
        self._period = period
        self._auto_adjust = auto_adjust
        self._ohlcv: pd.DataFrame | None = None
        self._info: dict[str, Any] | None = None
        self._sa_data: dict[str, Any] | None = None

    @property
    def ohlcv(self) -> pd.DataFrame:
        """Lazily fetch OHLCV data. First access downloads; subsequent accesses return cached data."""
        if self._ohlcv is None:
            # Try loading a snapshot first (gen_chart.py scenario)
            cached = MarketDataService.load_ohlcv_snapshot(
                self._ticker, run_dir=self._run_dir
            )
            if cached is not None:
                self._ohlcv = cached
            else:
                self._ohlcv = MarketDataService.fetch_ohlcv(
                    self._ticker,
                    period=self._period,
                    auto_adjust=self._auto_adjust,
                )
                # Save snapshot for other scripts in the same run_dir
                MarketDataService.save_ohlcv_snapshot(
                    self._ohlcv, self._ticker, run_dir=self._run_dir
                )
        return self._ohlcv

    @property
    def info(self) -> dict[str, Any]:
        """Lazily fetch yf.Ticker().info."""
        if self._info is None:
            self._info = MarketDataService.fetch_ticker_info(self._ticker)
        return self._info

    @property
    def stock_analysis(self) -> dict[str, Any]:
        """Lazily fetch StockAnalysis.com data."""
        if self._sa_data is None:
            self._sa_data = MarketDataService.fetch_stock_analysis(self._ticker)
        return self._sa_data

    def ohlcv_for_chart(self, months: int) -> pd.DataFrame:
        """Return the tail of OHLCV data covering approximately *months* months.

        Uses ~21 trading days per month as an approximation.
        """
        return self.ohlcv.tail(max(1, months * 21))
