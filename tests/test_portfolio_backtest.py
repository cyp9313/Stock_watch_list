from __future__ import annotations

import pandas as pd
import pytest

from portfolio_backtest import run_equal_weight_dca_backtest, scheduled_contribution_dates


def _prices():
    index = pd.date_range("2025-01-01", "2025-02-28", freq="B")
    return pd.DataFrame({
        "AAA": range(100, 100 + len(index)),
        "BBB": range(200, 200 + len(index)),
        "SPY": range(300, 300 + len(index)),
        "QQQ": range(400, 400 + len(index)),
    }, index=index, dtype=float)


def test_monthly_start_and_middle_schedule_dates():
    assert [item.date().isoformat() for item in scheduled_contribution_dates("2025-01-02", "2025-03-20", "monthly", "start")] == [
        "2025-02-01", "2025-03-01",
    ]
    assert [item.date().isoformat() for item in scheduled_contribution_dates("2025-01-02", "2025-03-20", "monthly", "middle")] == [
        "2025-01-17", "2025-02-21",
    ]


def test_weekly_dca_uses_friday_and_shifts_holiday_execution():
    prices = _prices().drop(pd.Timestamp("2025-01-03"))
    result = run_equal_weight_dca_backtest(prices, ["AAA", "BBB"], "2025-01-02", "2025-01-31", "weekly")

    assert result["scheduled_contributions"] == 5
    assert result["trade_events"][0]["scheduled_date"] == "2025-01-03"
    assert result["trade_events"][0]["trade_date"] == "2025-01-06"
    assert result["portfolio_tickers"] == ["AAA", "BBB"]
    assert {curve["key"] for curve in result["curves"]} == {"portfolio", "SPY", "QQQ"}
    assert len(result["buy_markers"]) == result["executed_contributions"]
    for curve in result["curves"]:
        if curve["key"] in {"SPY", "QQQ"}:
            first_return = next(value for value in curve["return_pct"] if value is not None)
            assert first_return == 0.0


def test_monthly_third_friday_rolls_to_next_trading_close():
    prices = _prices().drop(pd.Timestamp("2025-01-17"))
    result = run_equal_weight_dca_backtest(prices, ["AAA"], "2025-01-02", "2025-01-31", "monthly", "middle")

    assert result["trade_events"][0]["scheduled_date"] == "2025-01-17"
    assert result["trade_events"][0]["trade_date"] == "2025-01-20"


def test_short_window_uses_selected_start_as_one_initial_contribution():
    result = run_equal_weight_dca_backtest(_prices(), ["AAA"], "2025-01-21", "2025-01-23", "weekly")

    assert result["initial_contribution_fallback"] is True
    assert result["scheduled_contributions"] == 1
    assert result["trade_events"][0]["scheduled_date"] == "2025-01-21"


def test_equal_weight_dca_excludes_unavailable_portfolio_ticker_without_zeroing_result():
    prices = _prices().drop(columns=["BBB"])
    result = run_equal_weight_dca_backtest(prices, ["AAA", "BBB"], "2025-01-02", "2025-01-31", "weekly")

    assert result["portfolio_tickers"] == ["AAA"]
    assert result["unavailable_tickers"] == ["BBB"]
    assert result["curves"][0]["return_pct"][-1] is not None


def test_later_ipo_joins_at_next_contribution_without_consuming_pre_ipo_cash():
    prices = _prices()
    prices.loc[prices.index < pd.Timestamp("2025-01-20"), "BBB"] = float("nan")

    result = run_equal_weight_dca_backtest(prices, ["AAA", "BBB"], "2025-01-02", "2025-01-31", "weekly")

    allocations_by_date = {}
    for event in result["trade_events"]:
        allocations_by_date.setdefault(event["scheduled_date"], []).append(event)
    assert {event["ticker"] for event in allocations_by_date["2025-01-03"]} == {"AAA"}
    assert allocations_by_date["2025-01-03"][0]["amount"] == 1.0
    assert {event["ticker"] for event in allocations_by_date["2025-01-24"]} == {"AAA", "BBB"}
    assert {event["amount"] for event in allocations_by_date["2025-01-24"]} == {0.5}
    assert result["executed_contributions"] == result["scheduled_contributions"]


def test_backtest_rejects_invalid_window_and_empty_portfolio():
    with pytest.raises(ValueError, match="end date"):
        run_equal_weight_dca_backtest(_prices(), ["AAA"], "2025-01-31", "2025-01-02")
    with pytest.raises(ValueError, match="holding"):
        run_equal_weight_dca_backtest(_prices(), [], "2025-01-02", "2025-01-31")
