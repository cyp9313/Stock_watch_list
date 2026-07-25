from __future__ import annotations

import math
from pathlib import Path
import re

import pytest
from plotly.subplots import make_subplots

from kline_fibonacci import (
    add_fibonacci_overlays,
    calculate_auto_fibonacci,
    detect_pivot_candidates,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _wave_data():
    lows = [10, 9, 8, 9, 10, 11, 12, 11, 10, 9, 10, 12, 13, 12, 11, 10, 11, 12, 13, 14]
    highs = [value + 2 for value in lows]
    closes = [(low + high) / 2 for low, high in zip(lows, highs)]
    dates = [f"2026-01-{index + 1:02d}" for index in range(len(lows))]
    return dates, {"high": highs, "low": lows, "close": closes}


def _settings(*, retracement=True, extension=True, deviation=0.1, depth=4):
    return {
        "retracement": {"enabled": retracement, "deviation": deviation, "depth": depth},
        "extension": {"enabled": extension, "depth": depth},
    }


def test_retracement_uses_latest_bullish_leg_and_structured_levels():
    dates, ohlc = _wave_data()
    result = calculate_auto_fibonacci(dates, ohlc, _settings(extension=False))

    retracement = result["retracement"]
    assert retracement["direction"] == "bullish"
    assert [item["name"] for item in retracement["anchors"]] == ["A", "B"]
    assert retracement["anchors"][0]["pivot_type"] == "low"
    assert retracement["anchors"][1]["pivot_type"] == "high"
    assert retracement["developing"] is True
    assert retracement["levels"][0] == {"ratio": 0.0, "label": "0%", "price": 16.0, "kind": "retracement"}
    assert retracement["levels"][-1]["price"] == 10.0


def test_extension_finds_recent_valid_bullish_abc_and_uses_extension_kind():
    dates, ohlc = _wave_data()
    result = calculate_auto_fibonacci(dates, ohlc, _settings(retracement=False))

    extension = result["extension"]
    assert extension["direction"] == "bullish"
    assert [anchor["pivot_type"] for anchor in extension["anchors"]] == ["low", "high", "low"]
    assert extension["levels"][0]["price"] == extension["anchors"][2]["price"]
    level_1618 = next(level for level in extension["levels"] if level["ratio"] == 1.618)
    assert level_1618["kind"] == "extension"
    assert level_1618["price"] == pytest.approx(19.708)


def test_retracement_and_extension_support_bearish_swings():
    lows = [15, 16, 17, 16, 15, 14, 13, 14, 15, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6]
    dates = [f"2026-02-{index + 1:02d}" for index in range(len(lows))]
    ohlc = {
        "high": [value + 2 for value in lows],
        "low": lows,
        "close": [value + 1 for value in lows],
    }

    result = calculate_auto_fibonacci(dates, ohlc, _settings())

    assert result["retracement"]["direction"] == "bearish"
    assert result["retracement"]["anchors"][-1]["pivot_type"] == "low"
    assert next(level for level in result["retracement"]["levels"] if level["ratio"] == 0.5)["price"] == 12.5
    assert result["extension"]["direction"] == "bearish"
    assert next(level for level in result["extension"]["levels"] if level["ratio"] == 1.0)["price"] == 12.0


def test_extension_is_independent_from_retracement_deviation():
    dates, ohlc = _wave_data()
    low_filter = calculate_auto_fibonacci(dates, ohlc, _settings(deviation=0.1))["extension"]
    high_filter = calculate_auto_fibonacci(dates, ohlc, _settings(deviation=20.0))["extension"]

    assert low_filter == high_filter


def test_equal_extrema_keep_the_latest_candidate_in_a_plateau():
    candidates = detect_pivot_candidates([1, 2, 3, 3, 2, 1], [0, 0, 0, 0, 0, 0], depth=4)
    highs = [candidate for candidate in candidates if candidate["pivot_type"] == "high"]

    assert [candidate["index"] for candidate in highs] == [3]


def test_bad_ohlc_is_rejected_without_mutating_input():
    dates, ohlc = _wave_data()
    original = {key: list(value) for key, value in ohlc.items()}
    with pytest.raises(ValueError, match="match dates"):
        calculate_auto_fibonacci(dates, {**ohlc, "high": ohlc["high"][:-1]}, _settings())
    assert ohlc == original

    with pytest.raises(ValueError, match="no finite"):
        calculate_auto_fibonacci(dates, {"high": [math.inf] * len(dates), "low": [math.nan] * len(dates), "close": [math.nan] * len(dates)}, _settings())


def test_insufficient_data_returns_diagnostics_without_breaking_chart_result():
    result = calculate_auto_fibonacci(
        ["2026-01-01", "2026-01-02"],
        {"high": [2, 3], "low": [1, 2], "close": [1.5, 2.5]},
        _settings(),
    )

    assert result["retracement"] is None
    assert result["extension"] is None
    assert result["diagnostics"]["retracement_reason"]
    assert result["diagnostics"]["extension_reason"]


def test_plotly_overlay_uses_stable_legend_groups_and_anchor_based_segments():
    dates, ohlc = _wave_data()
    fibonacci = calculate_auto_fibonacci(dates, ohlc, _settings())
    figure = make_subplots(rows=1, cols=1)

    add_fibonacci_overlays(figure, fibonacci, dates, dark_mode=True)

    retracement = [trace for trace in figure.data if trace.legendgroup == "auto_fib_retracement"]
    extension = [trace for trace in figure.data if trace.legendgroup == "auto_fib_extension"]
    assert retracement[0].name == "Auto Fib Retracement"
    assert extension[0].name == "Auto Fib Extension"
    assert retracement[0].showlegend is True
    assert "A (Low)" in retracement[0].text
    assert "B (High)" in retracement[0].text
    assert all(trace.showlegend is False for trace in retracement[1:])
    assert retracement[1].x[0] == fibonacci["retracement"]["anchors"][0]["date"]
    assert any("R 0% (B)" in str(value) for trace in retracement[1:] for value in trace.text if value)
    assert any("R 100% (A)" in str(value) for trace in retracement[1:] for value in trace.text if value)
    assert any("R 61.8%" in str(value) for trace in retracement[1:] for value in trace.text if value)


def test_streamlit_paths_use_structured_auto_fibonacci_without_manual_anchor_state():
    single = (REPO_ROOT / "app_streamlit.py").read_text(encoding="utf-8")
    multi = (REPO_ROOT / "app_streamlit_multiuser.py").read_text(encoding="utf-8")

    for source in (single, multi):
        assert "from kline_fibonacci import add_fibonacci_overlays, calculate_auto_fibonacci" in source
        assert 'settings["fibonacci"]' in source
        assert "fibonacci=fibonacci" in source
        assert "fib_levels" not in source
        assert "fib_a" not in source
        assert "fib_b" not in source
        assert "fib_c" not in source
        assert "Calculate Fibonacci" not in source
        assert "with st.expander(\"Auto Fibonacci\"" not in source
    assert 'calculate_auto_fibonacci(chart_data["dates"], chart_data["ohlc"], settings["fibonacci"])' in multi


def test_indicator_apply_reruns_after_advancing_the_form_revision():
    for app_name in ("app_streamlit.py", "app_streamlit_multiuser.py"):
        source = (REPO_ROOT / app_name).read_text(encoding="utf-8")
        assert re.search(
            r'st\.session_state\["kline_indicator_form_revision"\] = form_revision \+ 1[\s\S]{0,700}?st\.rerun\(\)',
            source,
        ), app_name
