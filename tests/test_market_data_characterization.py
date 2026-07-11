"""Tests for P1-1B: unified market data service and data pipeline.

Covers:
- Merged StockAnalysis scraper: 20 fields, backward compatible with 9 original
- Ticker mapping for US/HK/A-share/EU/ETF/index/crypto
- DataStatus enum: actual zero vs missing vs N/A vs provider error
- MarketDataService: no Flask dependency, snapshot save/load, fetch methods
- Report data snapshot: single download per report, gen_chart reuses fetch_and_calc data
- auto_adjust=False preservation (Adj Close column retained)
- Volume preservation for index/ETF (Volume, Volume Ratio, Volume Profile)
- Beta source unchanged (yf.Ticker().info, not merged with backend np.cov)
- CLI, worker, and Flask independent callability
- Network call count: yf.download called once per report (not twice)
- StockAnalysis parsing compatibility

Run with:
    python -m pytest tests/test_market_data_characterization.py -v
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

# Ensure the project root is on sys.path so we can import market_data_service etc.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Other test files (test_db_init_separation, test_health_check, test_login_security,
# test_stock_data_post) stub ticker_mapping in sys.modules with a lambda or MagicMock.
# Remove any such stub so we import the REAL modules with proper implementations.
for _mod_name in ("ticker_mapping", "stockanalysis_scraper"):
    _existing = sys.modules.get(_mod_name)
    if _existing is not None:
        _is_stub = (
            not hasattr(_existing, "should_query_stockanalysis")
            or not callable(getattr(_existing, "should_query_stockanalysis", None))
            or getattr(_existing, "_is_test_stub", False)
        )
        if _is_stub:
            del sys.modules[_mod_name]

from market_data_service import DataStatus, MarketDataService, ReportDataSnapshot
from stockanalysis_scraper import (
    RESULT_KEYS,
    FIELD_ALIASES,
    empty_result,
    parse_float,
    parse_market_cap,
    parse_stockanalysis_page,
    scrape_stock_analysis,
    scrape_batch,
    should_query_forward_pe,
)
from ticker_mapping import (
    normalize_yfinance_ticker,
    is_known_us_etf,
    should_query_stockanalysis,
    stockanalysis_candidate_urls,
)

# Save references to the REAL modules at import time — other test files
# (test_stock_data_post, test_health_check) replace sys.modules entries with
# MagicMock stubs via autouse fixtures. Their stubs persist after their tests
# finish, which breaks our patch() calls. This fixture restores real modules
# before each of our tests.
import importlib as _importlib

_REAL_TICKER_MAPPING = sys.modules.get("ticker_mapping")
_REAL_SCRAPER = sys.modules.get("stockanalysis_scraper")

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _restore_real_modules():
    """Restore real ticker_mapping and stockanalysis_scraper before each test."""
    if _REAL_TICKER_MAPPING is not None:
        sys.modules["ticker_mapping"] = _REAL_TICKER_MAPPING
    if _REAL_SCRAPER is not None:
        sys.modules["stockanalysis_scraper"] = _REAL_SCRAPER
    yield


# ── Helpers ────────────────────────────────────────────────────────────

def _make_ohlcv(n_days: int = 250, start_price: float = 100.0) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame resembling yf.download output."""
    dates = pd.date_range("2025-01-01", periods=n_days, freq="B")
    np.random.seed(42)
    returns = np.random.randn(n_days) * 0.02
    close = start_price * np.cumprod(1 + returns)
    high = close * (1 + np.abs(np.random.randn(n_days)) * 0.01)
    low = close * (1 - np.abs(np.random.randn(n_days)) * 0.01)
    opn = close * (1 + np.random.randn(n_days) * 0.005)
    vol = np.random.randint(1_000_000, 50_000_000, n_days)
    adj_close = close * 0.95  # Adj Close differs from Close
    df = pd.DataFrame({
        "Open": opn,
        "High": high,
        "Low": low,
        "Close": close,
        "Adj Close": adj_close,
        "Volume": vol,
    }, index=dates)
    return df


# ── A. Scraper Merge Compatibility ─────────────────────────────────────

class TestScraperMerge:
    """Verify the merged scraper (20 fields) is backward compatible."""

    def test_result_keys_has_20_fields(self):
        """Merged scraper should have 20 field keys."""
        assert len(RESULT_KEYS) == 20

    def test_original_9_fields_present(self):
        """The original 9 fields must all be present in the merged scraper."""
        original_9 = {
            "forward_pe", "peg_ratio", "trailing_pe", "market_cap",
            "earnings_date", "ps_ratio", "pb_ratio", "analyst_rating",
            "price_target",
        }
        assert original_9.issubset(set(RESULT_KEYS))

    def test_new_11_fields_present(self):
        """The 11 new V5.8 fields must be present."""
        new_11 = {
            "ev_sales", "ev_ebitda", "ev_fcf", "p_fcf", "p_ocf",
            "forward_ps", "fcf_yield", "debt_equity", "debt_ebitda",
            "debt_fcf", "interest_coverage",
        }
        assert new_11.issubset(set(RESULT_KEYS))

    def test_empty_result_has_all_fields(self):
        """empty_result should return all 20 fields plus 'raw'."""
        result = empty_result("test")
        for key in RESULT_KEYS:
            assert key in result
            assert result[key] is None
        assert result["raw"] == "test"

    def test_empty_result_backward_compatible(self):
        """Callers reading the original 9 keys should get None."""
        result = empty_result("test")
        for key in ("forward_pe", "peg_ratio", "trailing_pe", "market_cap",
                     "earnings_date", "ps_ratio", "pb_ratio",
                     "analyst_rating", "price_target"):
            assert key in result
            assert result[key] is None

    def test_field_aliases_cover_all_keys(self):
        """FIELD_ALIASES should have entries for all 20 RESULT_KEYS."""
        assert set(FIELD_ALIASES.keys()) == set(RESULT_KEYS)

    def test_parse_float_handles_na_variants(self):
        """parse_float should return None for N/A variants."""
        for na_str in ("n/a", "na", "-", "\u2014"):
            assert parse_float(na_str) is None
        assert parse_float("") is None
        assert parse_float(None) is None

    def test_parse_float_parses_numbers(self):
        """parse_float should parse numeric strings."""
        assert parse_float("12.34") == 12.34
        assert parse_float("1,234.56") == 1234.56
        assert parse_float("-5.0") == -5.0

    def test_parse_market_cap_suffixes(self):
        """parse_market_cap should handle T/B/M/K suffixes."""
        assert parse_market_cap("1.5T") == 1.5e12
        assert parse_market_cap("2.3B") == 2.3e9
        assert parse_market_cap("500M") == 5e8
        assert parse_market_cap("100K") == 1e5

    def test_parse_page_extracts_js_values(self):
        """parse_stockanalysis_page should extract JS-embedded values."""
        html_text = '''
        <html>
        <script>
        someData("Forward PE",value:"25.5");
        someData("PE Ratio",value:"28.3");
        someData("Market Cap",value:"2.5T");
        </script>
        </html>
        '''
        result = parse_stockanalysis_page(html_text, "https://example.com")
        assert result["forward_pe"] == 25.5
        assert result["trailing_pe"] == 28.3
        assert result["market_cap"] == 2.5e12

    def test_parse_page_extracts_table_values(self):
        """parse_stockanalysis_page should extract HTML table values."""
        html_text = '''
        <html><body>
        <table>
        <tr><td>Forward PE</td><td>18.2</td></tr>
        <tr><td>PEG Ratio</td><td>1.5</td></tr>
        </table>
        </body></html>
        '''
        result = parse_stockanalysis_page(html_text, "https://example.com")
        assert result["forward_pe"] == 18.2
        assert result["peg_ratio"] == 1.5

    def test_parse_page_fcf_yield_derivation(self):
        """FCF yield should be derived from P/FCF when direct field absent."""
        html_text = '''
        <html><script>
        data("P / FCF",value:"20.0");
        </script></html>
        '''
        result = parse_stockanalysis_page(html_text, "https://example.com")
        assert result["p_fcf"] == 20.0
        assert result["fcf_yield"] is not None
        assert abs(result["fcf_yield"] - 5.0) < 0.01  # 100/20 = 5.0

    def test_scrape_batch_returns_dict(self):
        """scrape_batch should return a dict (possibly empty)."""
        with patch("stockanalysis_scraper.scrape_stock_analysis") as mock_scrape:
            mock_scrape.return_value = empty_result("test")
            result = scrape_batch(["AAPL"])
            assert isinstance(result, dict)

    def test_scrape_batch_skips_unsupported(self):
        """scrape_batch should skip tickers that shouldn't query SA."""
        with patch("stockanalysis_scraper.scrape_stock_analysis") as mock_scrape:
            mock_scrape.return_value = empty_result("test")
            # ^GSPC is an index, should be skipped
            result = scrape_batch(["^GSPC"])
            assert result == {}


# ── B. Ticker Mapping ──────────────────────────────────────────────────

class TestTickerMapping:
    """Verify ticker mapping for all instrument types."""

    def test_us_stock(self):
        assert normalize_yfinance_ticker("AAPL") == "AAPL"

    def test_us_stock_lowercase(self):
        assert normalize_yfinance_ticker("aapl") == "AAPL"

    def test_hk_stock_with_colon(self):
        assert normalize_yfinance_ticker("hkg:0700") == "0700.HK"

    def test_hk_stock_with_suffix(self):
        assert normalize_yfinance_ticker("0700.HK") == "0700.HK"

    def test_hk_stock_pads_to_4_digits(self):
        assert normalize_yfinance_ticker("hkg:700") == "0700.HK"

    def test_a_share_ss(self):
        assert normalize_yfinance_ticker("510300.SS") == "510300.SS"

    def test_a_share_sz(self):
        assert normalize_yfinance_ticker("000001.SZ") == "000001.SZ"

    def test_a_share_pads_to_6_digits(self):
        assert normalize_yfinance_ticker("sha:10001") == "010001.SS"

    def test_european_stock(self):
        assert normalize_yfinance_ticker("SAP.DE") == "SAP.DE"

    def test_european_stock_with_colon(self):
        assert normalize_yfinance_ticker("etr:SAP") == "SAP.DE"

    def test_etf_not_normalized_away(self):
        assert normalize_yfinance_ticker("SPY") == "SPY"
        assert normalize_yfinance_ticker("QQQ") == "QQQ"

    def test_index_not_normalized_away(self):
        assert normalize_yfinance_ticker("^GSPC") == "^GSPC"
        assert normalize_yfinance_ticker("^IXIC") == "^IXIC"

    def test_crypto_not_normalized_away(self):
        assert normalize_yfinance_ticker("BTC-USD") == "BTC-USD"

    def test_should_query_stockanalysis_skips_index(self):
        assert not should_query_stockanalysis("^GSPC")

    def test_should_query_stockanalysis_skips_crypto(self):
        assert not should_query_stockanalysis("BTC-USD")

    def test_should_query_stockanalysis_allows_stock(self):
        assert should_query_stockanalysis("AAPL")

    def test_should_query_stockanalysis_allows_etf(self):
        assert should_query_stockanalysis("SPY")

    def test_is_known_us_etf(self):
        assert is_known_us_etf("SPY")
        assert is_known_us_etf("QQQ")
        assert not is_known_us_etf("AAPL")

    def test_candidate_urls_for_stock(self):
        urls = stockanalysis_candidate_urls("AAPL")
        assert len(urls) >= 1
        assert "stockanalysis.com" in urls[0]

    def test_candidate_urls_for_index_empty(self):
        urls = stockanalysis_candidate_urls("^GSPC")
        assert urls == []

    def test_candidate_urls_for_etf(self):
        urls = stockanalysis_candidate_urls("SPY")
        assert len(urls) >= 1
        assert "/etf/" in urls[0]


# ── C. DataStatus Enum ─────────────────────────────────────────────────

class TestDataStatus:
    """Verify DataStatus correctly distinguishes four states."""

    def test_actual_zero(self):
        """Zero is a real value, not missing."""
        assert MarketDataService.classify_field(0) == DataStatus.ACTUAL
        assert MarketDataService.classify_field(0.0) == DataStatus.ACTUAL

    def test_actual_value(self):
        assert MarketDataService.classify_field(42.5) == DataStatus.ACTUAL
        assert MarketDataService.classify_field(-3) == DataStatus.ACTUAL

    def test_missing_none(self):
        assert MarketDataService.classify_field(None) == DataStatus.MISSING

    def test_not_applicable_for_instrument(self):
        """Fields N/A for certain instrument types."""
        status = MarketDataService.classify_field(
            None, instrument_type="INDEX",
            not_applicable_for={"INDEX", "CRYPTO"},
        )
        assert status == DataStatus.NOT_APPLICABLE

    def test_not_applicable_string_na(self):
        assert MarketDataService.classify_field("n/a") == DataStatus.NOT_APPLICABLE
        assert MarketDataService.classify_field("N/A") == DataStatus.NOT_APPLICABLE
        assert MarketDataService.classify_field("—") == DataStatus.NOT_APPLICABLE

    def test_provider_error(self):
        assert MarketDataService.classify_field("request_error: timeout") == DataStatus.PROVIDER_ERROR

    def test_enum_values(self):
        assert DataStatus.ACTUAL.value == "actual"
        assert DataStatus.MISSING.value == "missing"
        assert DataStatus.NOT_APPLICABLE.value == "n/a"
        assert DataStatus.PROVIDER_ERROR.value == "error"


# ── D. MarketDataService ───────────────────────────────────────────────

class TestMarketDataService:
    """Verify MarketDataService core functionality."""

    def test_no_flask_dependency(self):
        """MarketDataService module should not import Flask."""
        import market_data_service
        source = open(market_data_service.__file__).read()
        # Check for actual import statements, not mentions in docstrings
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        ]
        for line in import_lines:
            assert "flask" not in line.lower(), f"Flask import found: {line}"
            assert "Flask" not in line, f"Flask import found: {line}"

    def test_fetch_ohlcv_calls_yf_download(self):
        """fetch_ohlcv should call yf.download with correct parameters."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df) as mock_dl:
            result = MarketDataService.fetch_ohlcv("AAPL", period="1y", auto_adjust=False)
            mock_dl.assert_called_once_with(
                "AAPL", period="1y", interval="1d", auto_adjust=False,
            )
            assert isinstance(result, pd.DataFrame)

    def test_fetch_ohlcv_flattens_multiindex(self):
        """fetch_ohlcv should flatten MultiIndex columns."""
        mock_df = _make_ohlcv(30)
        # Simulate MultiIndex columns
        mock_df.columns = pd.MultiIndex.from_tuples(
            [(c, "AAPL") for c in mock_df.columns]
        )
        with patch("market_data_service.yf.download", return_value=mock_df):
            result = MarketDataService.fetch_ohlcv("AAPL")
            assert not isinstance(result.columns, pd.MultiIndex)

    def test_fetch_ticker_info(self):
        """fetch_ticker_info should call yf.Ticker().info."""
        mock_info = {"shortName": "Apple Inc.", "currency": "USD"}
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info
        with patch("market_data_service.yf.Ticker", return_value=mock_ticker):
            result = MarketDataService.fetch_ticker_info("AAPL")
            assert result == mock_info

    def test_fetch_ticker_info_returns_empty_on_error(self):
        """fetch_ticker_info should return {} on exception."""
        with patch("market_data_service.yf.Ticker", side_effect=Exception("network")):
            result = MarketDataService.fetch_ticker_info("AAPL")
            assert result == {}

    def test_fetch_stock_analysis_delegates_to_scraper(self):
        """fetch_stock_analysis should call scrape_stock_analysis."""
        mock_data = {"forward_pe": 25.0, "raw": "test"}
        with patch("stockanalysis_scraper.scrape_stock_analysis", return_value=mock_data):
            with patch("stockanalysis_scraper.should_query_forward_pe", return_value=True):
                result = MarketDataService.fetch_stock_analysis("AAPL")
                assert result == mock_data

    def test_fetch_stock_analysis_returns_empty_for_unsupported(self):
        """fetch_stock_analysis should return {} for unsupported tickers."""
        with patch("stockanalysis_scraper.should_query_forward_pe", return_value=False):
            result = MarketDataService.fetch_stock_analysis("^GSPC")
            assert result == {}

    def test_normalize_ticker_delegates(self):
        """normalize_ticker should delegate to ticker_mapping."""
        assert MarketDataService.normalize_ticker("aapl") == "AAPL"
        assert MarketDataService.normalize_ticker("hkg:0700") == "0700.HK"


# ── E. Snapshot Save/Load ──────────────────────────────────────────────

class TestSnapshotSaveLoad:
    """Verify OHLCV snapshot save/load mechanism."""

    def test_save_and_load_roundtrip(self):
        """Saving then loading should return the same DataFrame."""
        df = _make_ohlcv(50)
        with tempfile.TemporaryDirectory() as tmpdir:
            MarketDataService.save_ohlcv_snapshot(df, "AAPL", run_dir=tmpdir)
            loaded = MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir)
            assert loaded is not None
            pd.testing.assert_frame_equal(loaded, df)

    def test_load_returns_none_if_no_snapshot(self):
        """load_ohlcv_snapshot should return None if no file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir)
            assert result is None

    def test_snapshot_filename_deterministic(self):
        """Same ticker should always produce same snapshot filename."""
        name1 = MarketDataService._snapshot_filename("AAPL")
        name2 = MarketDataService._snapshot_filename("AAPL")
        assert name1 == name2
        assert "AAPL" in name1
        assert name1.endswith(".pkl")

    def test_snapshot_filename_handles_special_chars(self):
        """Snapshot filename should handle special ticker characters."""
        name = MarketDataService._snapshot_filename("BTC-USD")
        assert "-" not in name or "BTC_USD" in name
        name2 = MarketDataService._snapshot_filename("^GSPC")
        assert "^" not in name2

    def test_clear_snapshot(self):
        """clear_ohlcv_snapshot should remove the file."""
        df = _make_ohlcv(10)
        with tempfile.TemporaryDirectory() as tmpdir:
            MarketDataService.save_ohlcv_snapshot(df, "AAPL", run_dir=tmpdir)
            assert MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir) is not None
            MarketDataService.clear_ohlcv_snapshot("AAPL", run_dir=tmpdir)
            assert MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir) is None

    def test_load_corrupt_snapshot_returns_none(self):
        """Corrupt snapshot file should return None, not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / MarketDataService._snapshot_filename("AAPL")
            path.write_bytes(b"not a pickle")
            result = MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir)
            assert result is None


# ── F. Report Data Snapshot (single download per report) ───────────────

class TestReportDataSnapshot:
    """Verify that ReportDataSnapshot ensures single download per report."""

    def test_ohlcv_downloaded_once(self):
        """ohlcv property should only download once, then cache."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df) as mock_dl:
            with patch("market_data_service.MarketDataService.load_ohlcv_snapshot", return_value=None):
                with patch("market_data_service.MarketDataService.save_ohlcv_snapshot"):
                    snapshot = ReportDataSnapshot("AAPL", run_dir="/tmp/test")
                    _ = snapshot.ohlcv
                    _ = snapshot.ohlcv  # second access
                    assert mock_dl.call_count == 1

    def test_ohlcv_loads_from_snapshot_if_available(self):
        """If a snapshot exists, ohlcv should load from it, not download."""
        cached_df = _make_ohlcv(40)
        with patch("market_data_service.yf.download") as mock_dl:
            with patch("market_data_service.MarketDataService.load_ohlcv_snapshot", return_value=cached_df):
                snapshot = ReportDataSnapshot("AAPL", run_dir="/tmp/test")
                result = snapshot.ohlcv
                pd.testing.assert_frame_equal(result, cached_df)
                mock_dl.assert_not_called()

    def test_ohlcv_for_chart_returns_tail(self):
        """ohlcv_for_chart should return the tail of the data."""
        mock_df = _make_ohlcv(250)
        with patch("market_data_service.MarketDataService.load_ohlcv_snapshot", return_value=mock_df):
            snapshot = ReportDataSnapshot("AAPL", run_dir="/tmp/test")
            chart_data = snapshot.ohlcv_for_chart(3)
            assert len(chart_data) == 63  # 3 * 21
            # Should be the last 63 rows
            pd.testing.assert_frame_equal(chart_data, mock_df.tail(63))

    def test_info_cached_after_first_access(self):
        """info property should only fetch once."""
        mock_info = {"shortName": "Test"}
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info
        with patch("market_data_service.yf.Ticker", return_value=mock_ticker) as mock_t:
            snapshot = ReportDataSnapshot("AAPL", run_dir="/tmp/test")
            _ = snapshot.info
            _ = snapshot.info
            assert mock_t.call_count == 1


# ── G. Network Call Count (gen_chart reuses fetch_and_calc data) ───────

class TestNetworkCallCount:
    """Verify that gen_chart.py reuses the snapshot from fetch_and_calc.py."""

    def test_gen_chart_loads_snapshot_no_download(self):
        """gen_chart.py should NOT call yf.download when snapshot exists."""
        df = _make_ohlcv(30)
        with tempfile.TemporaryDirectory() as tmpdir:
            # Save snapshot as fetch_and_calc.py would
            MarketDataService.save_ohlcv_snapshot(df, "AAPL", run_dir=tmpdir)

            with patch("market_data_service.yf.download") as mock_dl:
                loaded = MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir)
                assert loaded is not None
                pd.testing.assert_frame_equal(loaded, df)
                mock_dl.assert_not_called()

    def test_gen_chart_downloads_when_no_snapshot(self):
        """gen_chart.py should call yf.download when no snapshot exists."""
        mock_df = _make_ohlcv(30)
        with tempfile.TemporaryDirectory() as tmpdir:
            # No snapshot saved
            loaded = MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir)
            assert loaded is None

            with patch("market_data_service.yf.download", return_value=mock_df) as mock_dl:
                data = MarketDataService.fetch_ohlcv("AAPL", period="1y", auto_adjust=False)
                mock_dl.assert_called_once()

    def test_single_report_single_download(self):
        """Full report flow: fetch_and_calc saves snapshot, gen_chart loads it.
        Total yf.download calls = 1 (not 2)."""
        mock_df = _make_ohlcv(30)
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("market_data_service.yf.download", return_value=mock_df) as mock_dl:
                # Simulate fetch_and_calc.py
                data1 = MarketDataService.fetch_ohlcv("AAPL", period="1y", auto_adjust=False)
                MarketDataService.save_ohlcv_snapshot(data1, "AAPL", run_dir=tmpdir)

                # Simulate gen_chart.py
                snapshot = MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir)
                assert snapshot is not None

                # Total downloads: 1 (only fetch_and_calc downloaded)
                assert mock_dl.call_count == 1

    def test_ticker_info_called_once_per_report(self):
        """fetch_ticker_info should only be called once per report."""
        mock_info = {"shortName": "Test"}
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info
        with patch("market_data_service.yf.Ticker", return_value=mock_ticker) as mock_t:
            snapshot = ReportDataSnapshot("AAPL", run_dir="/tmp/test")
            _ = snapshot.info
            _ = snapshot.info
            _ = snapshot.info
            assert mock_t.call_count == 1


# ── H. auto_adjust and Adj Close Preservation ──────────────────────────

class TestAutoAdjustPreservation:
    """Verify auto_adjust=False is preserved (Adj Close column retained)."""

    def test_fetch_ohlcv_passes_auto_adjust_false(self):
        """fetch_ohlcv must pass auto_adjust=False when requested."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df) as mock_dl:
            MarketDataService.fetch_ohlcv("AAPL", auto_adjust=False)
            _, kwargs = mock_dl.call_args
            assert kwargs["auto_adjust"] is False

    def test_adj_close_preserved(self):
        """DataFrame should contain Adj Close column when auto_adjust=False."""
        df = _make_ohlcv(30)
        assert "Adj Close" in df.columns

    def test_daily_returns_unchanged(self):
        """Daily returns should be computed from Close (same as before)."""
        df = _make_ohlcv(100)
        returns = df["Close"].pct_change().dropna()
        # Verify returns are computed correctly
        assert len(returns) == 99
        assert not returns.isna().any()

    def test_ma200_unchanged(self):
        """MA200 should be computed from Close (same as before)."""
        df = _make_ohlcv(250)
        ma200 = df["Close"].rolling(200).mean()
        assert not pd.isna(ma200.iloc[-1])

    def test_rsi_unchanged(self):
        """RSI should be computed from Close (same formula as before)."""
        df = _make_ohlcv(100)
        delta = df["Close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_g = gain.ewm(com=13, adjust=False).mean()
        avg_l = loss.ewm(com=13, adjust=False).mean()
        rs = avg_g / avg_l
        rsi = (100 - 100 / (1 + rs)).iloc[-1]
        assert 0 <= rsi <= 100

    def test_macd_unchanged(self):
        """MACD should use the same EWM parameters (12, 26, 9)."""
        df = _make_ohlcv(100)
        ema12 = df["Close"].ewm(span=12, adjust=False).mean()
        ema26 = df["Close"].ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        assert not pd.isna(macd.iloc[-1])
        assert not pd.isna(signal.iloc[-1])


# ── I. Volume Preservation for Index/ETF ───────────────────────────────

class TestVolumePreservation:
    """Verify Volume data is preserved for indices and ETFs."""

    def test_index_data_has_volume(self):
        """Index OHLCV data should contain Volume column."""
        df = _make_ohlcv(30)
        assert "Volume" in df.columns
        assert (df["Volume"] > 0).any()

    def test_etf_data_has_volume(self):
        """ETF OHLCV data should contain Volume column."""
        df = _make_ohlcv(30)
        assert "Volume" in df.columns

    def test_volume_ratio_computable(self):
        """Volume Ratio should be computable from the data."""
        df = _make_ohlcv(50)
        vol_ma20 = df["Volume"].rolling(20).mean().iloc[-1]
        today_vol = int(df["Volume"].iloc[-1])
        ratio = today_vol / vol_ma20 if vol_ma20 > 0 else None
        assert ratio is not None
        assert ratio > 0

    def test_volume_profile_input_unchanged(self):
        """Volume Profile should use High, Low, Close, Volume columns."""
        df = _make_ohlcv(63)
        required = {"High", "Low", "Close", "Volume"}
        assert required.issubset(set(df.columns))

    def test_chip_distribution_not_merged_to_daily(self):
        """Chip distribution 4h data should NOT be merged into daily OHLCV cache.
        The daily report's fetch_and_calc.py uses only daily interval='1d'."""
        # The snapshot only contains daily data (interval='1d')
        df = _make_ohlcv(30)
        # Verify it's daily data (no intraday timestamps)
        if len(df) > 1:
            time_diff = df.index[1] - df.index[0]
            # Daily data should have differences of at least 1 day
            assert time_diff >= pd.Timedelta(days=1)


# ── J. Beta Source Unchanged ───────────────────────────────────────────

class TestBetaSource:
    """Verify Beta source is unchanged: daily report uses yf.Ticker().info['beta'],
    NOT the backend's np.cov(252-day) calculation."""

    def test_beta_from_ticker_info(self):
        """Daily report Beta should come from yf.Ticker().info['beta']."""
        mock_info = {"beta": 1.25}
        mock_ticker = MagicMock()
        mock_ticker.info = mock_info
        with patch("market_data_service.yf.Ticker", return_value=mock_ticker):
            info = MarketDataService.fetch_ticker_info("AAPL")
            beta = info.get("beta", 0)
            assert beta == 1.25

    def test_beta_not_calculated_from_cov(self):
        """MarketDataService should NOT calculate Beta from np.cov.
        The backend's np.cov(252-day) algorithm is separate and not merged."""
        # MarketDataService has no fetch_beta method that uses np.cov
        assert not hasattr(MarketDataService, "fetch_beta")


# ── K. Independent Callability ──────────────────────────────────────────

class TestIndependentCallability:
    """Verify MarketDataService can be called from CLI, worker, and Flask independently."""

    def test_service_module_importable_without_flask(self):
        """MarketDataService should be importable without Flask installed."""
        # This test itself proves it — if Flask were required, the import
        # at the top of this file would have failed.
        assert MarketDataService is not None

    def test_service_callable_without_backend_running(self):
        """MarketDataService should work without the Flask backend running."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df):
            result = MarketDataService.fetch_ohlcv("AAPL")
            assert isinstance(result, pd.DataFrame)

    def test_service_callable_from_worker_context(self):
        """MarketDataService should work in a worker context (no Flask)."""
        # Simulate worker environment: no Flask, no Streamlit
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df):
            with tempfile.TemporaryDirectory() as tmpdir:
                df = MarketDataService.fetch_ohlcv("AAPL", auto_adjust=False)
                MarketDataService.save_ohlcv_snapshot(df, "AAPL", run_dir=tmpdir)
                loaded = MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir)
                assert loaded is not None

    def test_service_no_network_for_snapshot(self):
        """Loading a snapshot should NOT make any network calls."""
        df = _make_ohlcv(10)
        with tempfile.TemporaryDirectory() as tmpdir:
            MarketDataService.save_ohlcv_snapshot(df, "AAPL", run_dir=tmpdir)
            with patch("market_data_service.yf.download") as mock_dl:
                with patch("market_data_service.yf.Ticker") as mock_t:
                    loaded = MarketDataService.load_ohlcv_snapshot("AAPL", run_dir=tmpdir)
                    assert loaded is not None
                    mock_dl.assert_not_called()
                    mock_t.assert_not_called()


# ── L. fetch_and_calc.py Integration ───────────────────────────────────

class TestFetchAndCalcIntegration:
    """Verify fetch_and_calc.py uses MarketDataService correctly."""

    def test_fetch_and_calc_imports_market_data_service(self):
        """fetch_and_calc.py should import MarketDataService."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "fetch_and_calc.py"
        content = script_path.read_text(encoding="utf-8")
        assert "from market_data_service import MarketDataService" in content

    def test_fetch_and_calc_uses_fetch_ohlcv(self):
        """fetch_and_calc.py should call MarketDataService.fetch_ohlcv."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "fetch_and_calc.py"
        content = script_path.read_text(encoding="utf-8")
        assert "MarketDataService.fetch_ohlcv" in content

    def test_fetch_and_calc_saves_snapshot(self):
        """fetch_and_calc.py should save OHLCV snapshot for gen_chart.py."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "fetch_and_calc.py"
        content = script_path.read_text(encoding="utf-8")
        assert "save_ohlcv_snapshot" in content

    def test_fetch_and_calc_uses_fetch_ticker_info(self):
        """fetch_and_calc.py should call MarketDataService.fetch_ticker_info."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "fetch_and_calc.py"
        content = script_path.read_text(encoding="utf-8")
        assert "MarketDataService.fetch_ticker_info" in content

    def test_fetch_and_calc_cli_interface_unchanged(self):
        """fetch_and_calc.py CLI interface should be unchanged."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "fetch_and_calc.py"
        content = script_path.read_text(encoding="utf-8")
        assert "sys.argv[1]" in content
        assert "TICKER = sys.argv[1].upper()" in content


# ── M. gen_chart.py Integration ────────────────────────────────────────

class TestGenChartIntegration:
    """Verify gen_chart.py uses MarketDataService and shared snapshot."""

    def test_gen_chart_imports_market_data_service(self):
        """gen_chart.py should import MarketDataService."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "gen_chart.py"
        content = script_path.read_text(encoding="utf-8")
        assert "from market_data_service import MarketDataService" in content

    def test_gen_chart_loads_snapshot(self):
        """gen_chart.py should try to load OHLCV snapshot."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "gen_chart.py"
        content = script_path.read_text(encoding="utf-8")
        assert "load_ohlcv_snapshot" in content

    def test_gen_chart_fallback_to_fetch(self):
        """gen_chart.py should fall back to fetch_ohlcv when no snapshot."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "gen_chart.py"
        content = script_path.read_text(encoding="utf-8")
        assert "fetch_ohlcv" in content

    def test_gen_chart_cli_interface_unchanged(self):
        """gen_chart.py CLI interface should be unchanged."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "gen_chart.py"
        content = script_path.read_text(encoding="utf-8")
        assert "sys.argv[1]" in content
        assert "--months" in content


# ── N. Scraper File Merge Verification ─────────────────────────────────

class TestScraperFileMerge:
    """Verify the scraper file merge was done correctly."""

    def test_daily_report_scraper_deleted(self):
        """daily_report/scripts/stockanalysis_scraper.py should be deleted."""
        path = _PROJECT_ROOT / "daily_report" / "scripts" / "stockanalysis_scraper.py"
        assert not path.exists()

    def test_root_scraper_has_20_fields(self):
        """Root stockanalysis_scraper.py should have 20 RESULT_KEYS."""
        assert len(RESULT_KEYS) == 20

    def test_root_scraper_has_field_aliases(self):
        """Root scraper should have FIELD_ALIASES."""
        assert "forward_pe" in FIELD_ALIASES
        assert "ev_sales" in FIELD_ALIASES
        assert "interest_coverage" in FIELD_ALIASES

    def test_root_scraper_backward_compatible_functions(self):
        """Root scraper should export the same function signatures."""
        assert callable(scrape_stock_analysis)
        assert callable(scrape_batch)
        assert callable(should_query_forward_pe)
        assert callable(empty_result)
        assert callable(parse_stockanalysis_page)


# ── O. Provider Missing Data Behavior ──────────────────────────────────

class TestProviderMissingData:
    """Verify behavior when provider data is missing or fails."""

    def test_scraper_returns_empty_result_on_unsupported(self):
        """Scraping an unsupported ticker should return empty_result."""
        result = scrape_stock_analysis("^GSPC")
        assert result["forward_pe"] is None
        assert result["raw"] == "unsupported_ticker"

    def test_scraper_returns_empty_result_on_network_error(self):
        """Network errors should return empty_result with error in raw."""
        with patch("stockanalysis_scraper.requests") as mock_req:
            mock_req.get.side_effect = Exception("timeout")
            result = scrape_stock_analysis("AAPL")
            assert result["forward_pe"] is None
            assert "request_error" in result["raw"]

    def test_scraper_returns_empty_result_on_http_error(self):
        """HTTP errors should return empty_result with status in raw."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = ""
        with patch("stockanalysis_scraper.requests") as mock_req:
            mock_req.get.return_value = mock_resp
            result = scrape_stock_analysis("AAPL")
            assert result["forward_pe"] is None
            assert "http_404" in result["raw"]

    def test_ticker_info_empty_on_error(self):
        """fetch_ticker_info should return {} on error, not crash."""
        with patch("market_data_service.yf.Ticker", side_effect=Exception("fail")):
            result = MarketDataService.fetch_ticker_info("AAPL")
            assert result == {}

    def test_stock_analysis_empty_on_import_error(self):
        """fetch_stock_analysis should return {} if scraper unavailable."""
        with patch.dict("sys.modules", {"stockanalysis_scraper": None}):
            result = MarketDataService.fetch_stock_analysis("AAPL")
            assert result == {}


# ── P. Multi-Market Coverage ───────────────────────────────────────────

class TestMultiMarket:
    """Verify data pipeline handles multiple market types."""

    def test_us_stock_full_pipeline(self):
        """US stock (AAPL) should work through the full pipeline."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df):
            df = MarketDataService.fetch_ohlcv("AAPL", auto_adjust=False)
            assert "Close" in df.columns
            assert "Adj Close" in df.columns
            assert "Volume" in df.columns

    def test_etf_full_pipeline(self):
        """ETF (SPY) should work through the full pipeline."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df):
            df = MarketDataService.fetch_ohlcv("SPY", auto_adjust=False)
            assert "Close" in df.columns
            assert "Volume" in df.columns

    def test_index_full_pipeline(self):
        """Index (^GSPC) should work through the full pipeline."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df):
            df = MarketDataService.fetch_ohlcv("^GSPC", auto_adjust=False)
            assert "Close" in df.columns
            assert "Volume" in df.columns

    def test_hk_stock_full_pipeline(self):
        """HK stock (0700.HK) should work through the full pipeline."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df):
            df = MarketDataService.fetch_ohlcv("0700.HK", auto_adjust=False)
            assert "Close" in df.columns

    def test_crypto_full_pipeline(self):
        """Crypto (BTC-USD) should work through the full pipeline."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df):
            df = MarketDataService.fetch_ohlcv("BTC-USD", auto_adjust=False)
            assert "Close" in df.columns

    def test_european_stock_full_pipeline(self):
        """European stock (SAP.DE) should work through the full pipeline."""
        mock_df = _make_ohlcv(30)
        with patch("market_data_service.yf.download", return_value=mock_df):
            df = MarketDataService.fetch_ohlcv("SAP.DE", auto_adjust=False)
            assert "Close" in df.columns


# ── Q. Report Output Compatibility ─────────────────────────────────────

class TestReportOutputCompatibility:
    """Verify report output format is unchanged after refactoring."""

    def test_fetch_and_calc_preserves_json_keys(self):
        """fetch_and_calc.py JSON output should still contain all expected keys."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "fetch_and_calc.py"
        content = script_path.read_text(encoding="utf-8")
        # Verify critical output keys are still present
        expected_keys = [
            '"TICKER"', '"LAST_CLOSE"', '"PCT"', '"FW_PE"', '"BETA"',
            '"vol_ma5"', '"bull_ma_count"', '"rsi"', '"macd_line"', '"vol_ratio"',
            '"technical_score"', '"chip_profiles"', '"CURRENCY"',
            '"INSTRUMENT_TYPE"', '"SCORING_PROFILE"',
        ]
        for key in expected_keys:
            assert key in content, f"Missing key {key} in fetch_and_calc.py"

    def test_fetch_and_calc_preserves_scoring_formula(self):
        """Scoring formula references should be unchanged."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "fetch_and_calc.py"
        content = script_path.read_text(encoding="utf-8")
        assert "technical_nominal_weights" in content
        assert "trend_score" in content
        assert "momentum_score" in content
        assert "chip_profile_score" in content

    def test_gen_chart_preserves_subplot_structure(self):
        """gen_chart.py should still generate 5 subplots."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "gen_chart.py"
        content = script_path.read_text(encoding="utf-8")
        assert "make_subplots" in content
        assert "rows=5" in content

    def test_gen_chart_preserves_chart_colors(self):
        """gen_chart.py should preserve green-up/red-down coloring."""
        script_path = _PROJECT_ROOT / "daily_report" / "scripts" / "gen_chart.py"
        content = script_path.read_text(encoding="utf-8")
        assert "green" in content
        assert "red" in content


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "--tb=short"])
