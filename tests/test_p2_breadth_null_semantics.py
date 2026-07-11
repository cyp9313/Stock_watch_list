"""P2-11 tests: Market Breadth missing-value semantics.

Verifies that:
1. build_breadth_chart_payload returns None (not 0) for NaN MA ratio values
2. build_breadth_summary_rows returns None (not np.nan) for missing values
3. No "else 0" pattern remains in breadth chart payload code
4. No fillna(0) on 1D% in treemap code
5. JSON serialization produces valid null (not NaN) for missing values
6. Treemap source code handles NaN 1D% with "N/A" text
7. No np.nan in breadth summary rows (replaced with None for valid JSON)
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_FILE = REPO_ROOT / "stock_watch_list_back_end.py"
MULTIUSER_FILE = REPO_ROOT / "app_streamlit_multiuser.py"
SINGLEUSER_FILE = REPO_ROOT / "app_streamlit.py"

_BACKEND_SOURCE = BACKEND_FILE.read_text(encoding="utf-8")
_BACKEND_TREE = ast.parse(_BACKEND_SOURCE)

_MULTIUSER_SOURCE = MULTIUSER_FILE.read_text(encoding="utf-8")
_SINGLEUSER_SOURCE = SINGLEUSER_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helper: extract a function from source as executable code
# ---------------------------------------------------------------------------

def _extract_func_source(source: str, func_name: str) -> str:
    """Return the source code of a top-level function."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.unparse(node)
    raise ValueError(f"Function {func_name} not found")


# ---------------------------------------------------------------------------
# Structural tests: verify code patterns in source files
# ---------------------------------------------------------------------------

class TestNoElseZeroInBreadthChartPayload:
    """The 'else 0' pattern must not exist in breadth chart payload code."""

    def test_no_else_zero_in_backend(self):
        """No 'else 0 for x in chart_df' pattern in stock_watch_list_back_end.py."""
        assert "else 0 for x in chart_df" not in _BACKEND_SOURCE, (
            "Found 'else 0' pattern in breadth chart payload -- "
            "P2-11 requires NaN values to be None, not 0"
        )

    def test_else_none_exists_in_breadth_chart_payload(self):
        """'else None for x in chart_df' must exist in build_breadth_chart_payload."""
        assert "else None for x in chart_df" in _BACKEND_SOURCE, (
            "Expected 'else None for x in chart_df' in breadth chart payload"
        )


class TestNoNanInSummaryRows:
    """np.nan must not be used in breadth summary rows (should be None for valid JSON)."""

    def test_no_np_nan_in_summary_rows(self):
        """No 'np.nan' used as a dict value in breadth summary rows."""
        # Find the build_breadth_summary_rows function source
        func_src = _extract_func_source(_BACKEND_SOURCE, "build_breadth_summary_rows")
        # 'YTD%': np.nan or 'Volume_Ratio': np.nan would be invalid JSON
        # np.isnan() in conditions is fine, and local var = ... else np.nan is fine
        assert "'YTD%': np.nan" not in func_src, (
            "Found 'YTD%': np.nan in build_breadth_summary_rows -- "
            "P2-11 requires None for valid JSON serialization"
        )
        assert "'Volume_Ratio': np.nan" not in func_src, (
            "Found 'Volume_Ratio': np.nan in build_breadth_summary_rows"
        )
        assert "'Diff_BB_Up%': np.nan" not in func_src, (
            "Found 'Diff_BB_Up%': np.nan in build_breadth_summary_rows"
        )
        assert "None" in func_src, (
            "Expected 'None' in build_breadth_summary_rows for missing values"
        )

    def test_no_np_nan_in_dead_code_summary(self):
        """No np.nan in the dead-code summary rows section either."""
        # The dead code section also has the same pattern
        count_np_nan = _BACKEND_SOURCE.count('"YTD%": np.nan')
        count_none = _BACKEND_SOURCE.count('"YTD%": None')
        assert count_np_nan == 0, (
            f"Found {count_np_nan} occurrences of 'np.nan' in YTD% -- should be None"
        )
        assert count_none >= 2, (
            f"Expected at least 2 'None' for YTD% (active + dead code), got {count_none}"
        )


class TestNoFillnaZeroOnTreemap:
    """fillna(0) must not be used on 1D% in treemap code."""

    def test_no_fillna_zero_in_multiuser(self):
        """No '.fillna(0)' on 1D% in app_streamlit_multiuser.py."""
        assert 'rows["1D%"] = pd.to_numeric(rows["1D%"], errors="coerce").fillna(0)' not in _MULTIUSER_SOURCE, (
            "Found fillna(0) on 1D% in multiuser treemap -- "
            "P2-11 requires NaN values to be preserved, not masked as 0"
        )

    def test_no_fillna_zero_in_singleuser(self):
        """No '.fillna(0)' on 1D% in app_streamlit.py."""
        assert 'rows["1D%"] = pd.to_numeric(rows["1D%"], errors="coerce").fillna(0)' not in _SINGLEUSER_SOURCE, (
            "Found fillna(0) on 1D% in singleuser treemap -- "
            "P2-11 requires NaN values to be preserved, not masked as 0"
        )

    def test_na_handling_exists_in_multiuser(self):
        """NaN-aware handling exists in multiuser treemap."""
        assert 'pct_text = "N/A"' in _MULTIUSER_SOURCE, (
            "Expected 'N/A' text handling for NaN 1D% in multiuser treemap"
        )

    def test_na_handling_exists_in_singleuser(self):
        """NaN-aware handling exists in singleuser treemap."""
        assert 'pct_text = "N/A"' in _SINGLEUSER_SOURCE, (
            "Expected 'N/A' text handling for NaN 1D% in singleuser treemap"
        )


# ---------------------------------------------------------------------------
# Behavior tests: build_breadth_chart_payload
# ---------------------------------------------------------------------------

class TestBreadthChartPayloadNaNToNone:
    """Test that build_breadth_chart_payload returns None for NaN values."""

    @staticmethod
    def _make_func():
        """Extract and compile build_breadth_chart_payload for isolated testing."""
        func_src = _extract_func_source(_BACKEND_SOURCE, "build_breadth_chart_payload")
        ns = {"np": np, "pd": pd}
        exec(func_src, ns)
        return ns["build_breadth_chart_payload"]

    def test_nan_becomes_none_not_zero(self):
        """NaN values in MA ratio columns must be None in output, not 0."""
        func = self._make_func()
        # Create a DataFrame with some NaN values
        dates = pd.date_range("2024-01-01", periods=10, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [50.0, 55.0, np.nan, 60.0, np.nan, 65.0, 70.0, np.nan, 75.0, 80.0],
            "50MA_Ratio": [40.0, np.nan, np.nan, 45.0, 50.0, np.nan, 55.0, 60.0, np.nan, 65.0],
            "200MA_Ratio": [30.0, 35.0, 40.0, np.nan, np.nan, np.nan, 45.0, 50.0, 55.0, 60.0],
        }, index=dates)

        result = func(df)

        for key in ["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"]:
            values = result[key]
            assert len(values) == 10
            # Check that NaN positions are None, not 0
            for i, v in enumerate(values):
                if np.isnan(df[key].iloc[i]):
                    assert v is None, (
                        f"{key}[{i}] is {v} (expected None for NaN input)"
                    )
                else:
                    assert v == round(float(df[key].iloc[i]), 2)

    def test_all_nan_column(self):
        """A column that is entirely NaN must produce all None values."""
        func = self._make_func()
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [np.nan] * 5,
            "50MA_Ratio": [50.0] * 5,
            "200MA_Ratio": [30.0] * 5,
        }, index=dates)

        result = func(df)

        assert all(v is None for v in result["20MA_Ratio"])
        assert all(v == 50.0 for v in result["50MA_Ratio"])
        assert all(v == 30.0 for v in result["200MA_Ratio"])

    def test_no_zero_values_for_nan(self):
        """Ensure no 0 values appear where NaN was in input."""
        func = self._make_func()
        dates = pd.date_range("2024-01-01", periods=3, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [np.nan, np.nan, np.nan],
            "50MA_Ratio": [np.nan, np.nan, np.nan],
            "200MA_Ratio": [np.nan, np.nan, np.nan],
        }, index=dates)

        result = func(df)

        for key in ["20MA_Ratio", "50MA_Ratio", "200MA_Ratio"]:
            for v in result[key]:
                assert v is None, f"{key} contains {v} instead of None"
                assert v != 0, f"{key} contains 0 instead of None"

    def test_json_serialization_no_nan(self):
        """The payload must serialize to valid JSON without NaN tokens."""
        func = self._make_func()
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [50.0, np.nan, 60.0, np.nan, 70.0],
            "50MA_Ratio": [40.0, 45.0, np.nan, np.nan, 55.0],
            "200MA_Ratio": [30.0, 35.0, 40.0, 45.0, np.nan],
        }, index=dates)

        result = func(df)

        # json.dumps with allow_nan=False must not raise
        json_str = json.dumps(result, allow_nan=False)
        assert "NaN" not in json_str, "JSON contains invalid NaN token"
        assert "null" in json_str, "JSON should contain null for None values"

    def test_json_serialization_no_nan_for_index(self):
        """Index data also uses None for missing values (not 0)."""
        func = self._make_func()
        dates = pd.date_range("2024-01-01", periods=3, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [50.0, 60.0, 70.0],
            "50MA_Ratio": [40.0, 45.0, 55.0],
            "200MA_Ratio": [30.0, 35.0, 40.0],
        }, index=dates)

        result = func(df)

        json_str = json.dumps(result, allow_nan=False)
        assert "NaN" not in json_str


# ---------------------------------------------------------------------------
# Behavior tests: build_breadth_summary_rows
# ---------------------------------------------------------------------------

class TestBreadthSummaryRowsNone:
    """Test that build_breadth_summary_rows returns None for missing values."""

    @staticmethod
    def _make_func():
        """Extract and compile build_breadth_summary_rows for isolated testing."""
        func_src = _extract_func_source(_BACKEND_SOURCE, "build_breadth_summary_rows")
        ns = {"np": np, "pd": pd}
        exec(func_src, ns)
        return ns["build_breadth_summary_rows"]

    def test_missing_values_are_none_not_nan(self):
        """Missing values must be None, not np.nan."""
        func = self._make_func()
        # Only 1 data point → chg_1d, chg_5d, chg_20d will all be NaN
        dates = pd.date_range("2024-01-01", periods=1, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [50.0],
            "50MA_Ratio": [40.0],
            "200MA_Ratio": [30.0],
        }, index=dates)

        result = func(df)

        assert len(result) == 3  # one row per MA ratio
        for row in result:
            assert row["1D%"] is None, f"1D% is {row['1D%']} (expected None)"
            assert row["5D%"] is None, f"5D% is {row['5D%']} (expected None)"
            assert row["1M%"] is None, f"1M% is {row['1M%']} (expected None)"
            assert row["YTD%"] is None
            assert row["Volume_Ratio"] is None
            assert row["Diff_BB_Up%"] is None
            assert row["Diff_BB_Low%"] is None
            for n in [5, 10, 20, 50, 100, 200]:
                assert row[f"Diff_EMA{n}%"] is None

    def test_valid_values_are_numeric(self):
        """When enough data exists, values should be numeric (not None)."""
        func = self._make_func()
        dates = pd.date_range("2024-01-01", periods=30, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": list(range(30, 0, -1)),
            "50MA_Ratio": list(range(50, 20, -1)),
            "200MA_Ratio": list(range(200, 170, -1)),
        }, index=dates)

        result = func(df)

        assert len(result) == 3
        for row in result:
            assert isinstance(row["1D%"], (int, float)), f"1D% is {type(row['1D%'])}"
            assert isinstance(row["5D%"], (int, float)), f"5D% is {type(row['5D%'])}"
            assert isinstance(row["1M%"], (int, float)), f"1M% is {type(row['1M%'])}"
            # These are always None (not applicable for breadth)
            assert row["YTD%"] is None
            assert row["Volume_Ratio"] is None

    def test_json_serialization_valid(self):
        """Summary rows must serialize to valid JSON without NaN tokens."""
        func = self._make_func()
        dates = pd.date_range("2024-01-01", periods=1, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [50.0],
            "50MA_Ratio": [40.0],
            "200MA_Ratio": [30.0],
        }, index=dates)

        result = func(df)

        # Must not raise with allow_nan=False
        json_str = json.dumps(result, allow_nan=False)
        assert "NaN" not in json_str, "JSON contains invalid NaN token"
        assert "null" in json_str, "JSON should contain null for None values"

    def test_json_serialization_with_valid_data(self):
        """Summary rows with valid data also serialize correctly."""
        func = self._make_func()
        dates = pd.date_range("2024-01-01", periods=30, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": list(range(30, 0, -1)),
        }, index=dates)

        result = func(df)

        json_str = json.dumps(result, allow_nan=False)
        assert "NaN" not in json_str

    def test_empty_columns_skipped(self):
        """Columns not in the DataFrame are skipped."""
        func = self._make_func()
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [50.0, 55.0, 60.0, 65.0, 70.0],
        }, index=dates)

        result = func(df)

        assert len(result) == 1  # only 20MA_Ratio exists
        assert result[0]["Ticker"] == "20MA_Ratio"

    def test_prefix_applied(self):
        """Ticker prefix and display prefix are applied correctly."""
        func = self._make_func()
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [50.0, 55.0, 60.0, 65.0, 70.0],
        }, index=dates)

        result = func(df, ticker_prefix="SP500_", display_prefix="S&P 500 ")

        assert result[0]["Ticker"] == "SP500_20MA_Ratio"
        assert result[0]["Name"] == "S&P 500 20MA_Ratio"


# ---------------------------------------------------------------------------
# Treemap source code tests
# ---------------------------------------------------------------------------

class TestTreemapNaHandling:
    """Verify treemap source code handles NaN 1D% correctly."""

    def test_multiuser_treemap_has_valid_mask(self):
        """Multiuser treemap uses valid_mask to filter NaN before averaging."""
        assert "valid_mask" in _MULTIUSER_SOURCE, (
            "Expected valid_mask pattern in multiuser treemap for NaN-aware sector averaging"
        )

    def test_singleuser_treemap_has_valid_mask(self):
        """Singleuser treemap uses valid_mask to filter NaN before averaging."""
        assert "valid_mask" in _SINGLEUSER_SOURCE, (
            "Expected valid_mask pattern in singleuser treemap for NaN-aware sector averaging"
        )

    def test_multiuser_treemap_has_pct_raw_check(self):
        """Multiuser treemap checks pd.notna(pct_raw) before using 1D%."""
        assert "pct_raw" in _MULTIUSER_SOURCE, (
            "Expected pct_raw variable in multiuser treemap for NaN-aware stock display"
        )
        assert 'pd.notna(pct_raw)' in _MULTIUSER_SOURCE, (
            "Expected pd.notna(pct_raw) check in multiuser treemap"
        )

    def test_singleuser_treemap_has_pct_raw_check(self):
        """Singleuser treemap checks pd.notna(pct_raw) before using 1D%."""
        assert "pct_raw" in _SINGLEUSER_SOURCE, (
            "Expected pct_raw variable in singleuser treemap for NaN-aware stock display"
        )
        assert 'pd.notna(pct_raw)' in _SINGLEUSER_SOURCE, (
            "Expected pd.notna(pct_raw) check in singleuser treemap"
        )

    def test_multiuser_no_float_row_1d_directly(self):
        """Multiuser treemap must not do 'pct = float(row[\"1D%\"])' without NaN check."""
        assert 'pct = float(row["1D%"])' not in _MULTIUSER_SOURCE, (
            "Found direct 'float(row[\"1D%\"])' without NaN check -- "
            "this would crash or produce 'nan%' for missing 1D% values"
        )

    def test_singleuser_no_float_row_1d_directly(self):
        """Singleuser treemap must not do 'pct = float(row[\"1D%\"])' without NaN check."""
        assert 'pct = float(row["1D%"])' not in _SINGLEUSER_SOURCE, (
            "Found direct 'float(row[\"1D%\"])' without NaN check -- "
            "this would crash or produce 'nan%' for missing 1D% values"
        )


# ---------------------------------------------------------------------------
# Regression: verify valid data still works correctly
# ---------------------------------------------------------------------------

class TestValidDataRegression:
    """Ensure valid (non-NaN) data is not affected by the changes."""

    def test_chart_payload_valid_data(self):
        """All-valid data produces correct numeric output."""
        func = TestBreadthChartPayloadNaNToNone._make_func()
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [50.0, 55.0, 60.0, 65.0, 70.0],
            "50MA_Ratio": [40.0, 45.0, 50.0, 55.0, 60.0],
            "200MA_Ratio": [30.0, 35.0, 40.0, 45.0, 50.0],
        }, index=dates)

        result = func(df)

        assert result["20MA_Ratio"] == [50.0, 55.0, 60.0, 65.0, 70.0]
        assert result["50MA_Ratio"] == [40.0, 45.0, 50.0, 55.0, 60.0]
        assert result["200MA_Ratio"] == [30.0, 35.0, 40.0, 45.0, 50.0]
        assert len(result["index"]) == 5

    def test_summary_rows_valid_data(self):
        """All-valid data produces correct numeric output."""
        func = TestBreadthSummaryRowsNone._make_func()
        dates = pd.date_range("2024-01-01", periods=30, freq="B")
        df = pd.DataFrame({
            "20MA_Ratio": [float(i) for i in range(30, 0, -1)],
            "50MA_Ratio": [float(i) for i in range(50, 20, -1)],
            "200MA_Ratio": [float(i) for i in range(200, 170, -1)],
        }, index=dates)

        result = func(df)

        assert len(result) == 3
        for row in result:
            assert isinstance(row["Price"], (int, float))
            assert isinstance(row["1D%"], (int, float))
            assert isinstance(row["5D%"], (int, float))
            assert isinstance(row["1M%"], (int, float))
            assert isinstance(row["1D%"], (int, float))
