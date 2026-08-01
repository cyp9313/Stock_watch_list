import numpy as np
import pandas as pd

from market_screener import STRATEGIES, market_profile_for_ticker, run_screener, screening_benchmark_tickers


def _market_frame():
    index = pd.date_range("2024-01-01", periods=300, freq="B")
    benchmark = np.linspace(100, 130, len(index))
    trend = np.linspace(10, 50, len(index))
    breakout = np.linspace(20, 32, len(index))
    breakout[-1] = 36
    reversal = np.linspace(100, 60, len(index))
    reversal[-3:] = [60.0, 59.7, 60.5]
    close = pd.DataFrame({"^GSPC": benchmark, "TREND": trend, "BREAK": breakout, "REVERSE": reversal}, index=index)
    volume = pd.DataFrame(100_000, index=index, columns=close.columns)
    volume["BREAK"] = 100_000
    volume.iloc[-1, volume.columns.get_loc("BREAK")] = 300_000
    return pd.concat({"Adj Close": close, "Volume": volume}, axis=1)


def test_three_strategies_return_ranked_candidates_from_shared_frame():
    result = run_screener(_market_frame(), {"TREND": "S&P 500", "BREAK": "User watchlist", "REVERSE": "User watchlist"})

    assert result["version"]
    assert {item["key"] for item in result["strategies"]} == set(STRATEGIES)
    by_key = {item["key"]: item for item in result["strategies"]}
    assert any(row["Ticker"] == "TREND" for row in by_key["trend_quality"]["candidates"])
    assert any(row["Ticker"] == "BREAK" for row in by_key["volume_breakout"]["candidates"])
    assert any(row["Ticker"] == "REVERSE" for row in by_key["oversold_reversal"]["candidates"])
    for strategy in result["strategies"]:
        scores = [row["Score"] for row in strategy["candidates"]]
        assert scores == sorted(scores, reverse=True)


def test_insufficient_or_illiquid_data_is_explicitly_excluded():
    frame = _market_frame()
    frame[("Adj Close", "SHORT")] = frame[("Adj Close", "TREND")].iloc[-100:].reindex(frame.index)
    frame[("Volume", "SHORT")] = 1
    result = run_screener(frame, {"SHORT": "User watchlist"})

    for strategy in result["strategies"]:
        assert strategy["candidates"] == []
        assert strategy["excluded"]


def test_local_market_profile_uses_cny_thresholds_and_local_benchmark():
    assert market_profile_for_ticker("600519.SS") == "CN"
    assert screening_benchmark_tickers(["600519.SS", "0700.HK", "AAPL"]) == ["000001.SS", "^GSPC", "^HSI"]

    index = pd.date_range("2024-01-01", periods=300, freq="B")
    close = pd.DataFrame({
        "000001.SS": np.linspace(3000, 3400, len(index)),
        "600001.SS": np.linspace(4.0, 4.5, len(index)),
    }, index=index)
    volume = pd.DataFrame({"000001.SS": 1_000_000, "600001.SS": 11_000_000}, index=index)
    frame = pd.concat({"Adj Close": close, "Volume": volume}, axis=1)
    result = run_screener(frame, {"600001.SS": "User watchlist"})
    trend = next(item for item in result["strategies"] if item["key"] == "trend_quality")
    assert trend["candidates"] == []
    assert any("CNY 50,000,000" in reason for reason in trend["excluded"])


def test_factor_score_separates_base_score_and_risk_deduction():
    result = run_screener(
        _market_frame(),
        {"TREND": "S&P 500"},
        {"TREND": {"trailing_pe": 25, "forward_pe": 18, "pb_ratio": 12, "market_cap": 1_000_000_000}},
    )
    trend = next(item for item in result["strategies"] if item["key"] == "trend_quality")
    row = next(row for row in trend["candidates"] if row["Ticker"] == "TREND")
    assert row["Base Score"] >= row["Score"]
    assert row["Risk Deduction"] >= 2
    assert {"20D Relative%", "60D Relative%", "120D Relative%"}.issubset(row["Metrics"])
    assert row["Metrics"]["PE TTM"] == 25
    assert row["Metrics"]["Forward PE"] == 18
    assert not any("主题热度" in name for name in row["Factor Scores"])
