from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from stock_watch_list_back_end import (  # noqa: E402
    RELATIVE_RETURN_BENCHMARK,
    _is_legacy_truncated_ticker_name,
    _short_ticker_name,
    compute_rsi_series,
    compute_relative_return_scores,
)


def test_relative_return_scores_use_sp500_index_as_default_benchmark():
    assert RELATIVE_RETURN_BENCHMARK == "^GSPC"


def test_relative_return_scores_are_excess_return_percentages():
    dates = pd.bdate_range("2026-01-01", periods=130)
    data = pd.DataFrame(
        {
            "^GSPC": np.full(len(dates), 100.0),
            "AAA": np.full(len(dates), 100.0),
            "BBB": np.full(len(dates), 100.0),
        },
        index=dates,
    )
    data.iloc[-1] = {"^GSPC": 110.0, "AAA": 130.0, "BBB": 115.0}

    scores = compute_relative_return_scores(data, ["AAA", "BBB", "^GSPC"])

    for column in ("20D Rel%", "60D Rel%", "120D Rel%"):
        assert scores["AAA"][column] == pytest.approx(20.0)
        assert scores["BBB"][column] == pytest.approx(5.0)
        assert scores["^GSPC"][column] == pytest.approx(0.0)


def test_compute_rsi_series_uses_14_day_average_gain_loss():
    prices = pd.Series(
        [100, 101, 102, 101, 103, 104, 103, 105, 106, 107, 106, 108, 109, 110, 111],
        dtype="float64",
    )

    rsi = compute_rsi_series(prices)

    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    expected = 100 - (100 / (1 + gain / loss))
    assert rsi.iloc[-1] == pytest.approx(expected.iloc[-1])


def test_ticker_names_are_not_truncated_before_display():
    full_name = "Taiwan Semiconductor Manufacturing Company Limited"

    assert _short_ticker_name(full_name) == full_name
    assert _is_legacy_truncated_ticker_name("Taiwan Semiconductor Manu...")
    assert not _is_legacy_truncated_ticker_name(full_name)
