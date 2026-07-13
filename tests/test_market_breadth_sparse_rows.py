import numpy as np
import pandas as pd

from stock_watch_list_back_end import calculate_market_breadth


def test_market_breadth_drops_sparse_rows_before_rolling_ma():
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    historical_dates = pd.date_range("2025-01-01", periods=204, freq="D")
    sparse_date = historical_dates[-1] + pd.Timedelta(days=1)
    final_date = historical_dates[-1] + pd.Timedelta(days=2)
    clean_dates = historical_dates.append(pd.DatetimeIndex([final_date]))
    clean_prices = pd.DataFrame(
        {
            symbol: np.arange(100 + i, 100 + i + len(clean_dates), dtype=float)
            for i, symbol in enumerate(symbols)
        },
        index=clean_dates,
    )

    sparse_row = pd.DataFrame(
        [[clean_prices.iloc[-2, 0], clean_prices.iloc[-2, 1], np.nan, np.nan, np.nan]],
        index=[sparse_date],
        columns=symbols,
    )
    prices_with_sparse_row = (
        pd.concat([clean_prices.iloc[:-1], sparse_row, clean_prices.iloc[-1:]])
        .sort_index()
    )

    breadth = calculate_market_breadth(prices_with_sparse_row, symbols)

    assert sparse_date not in breadth.index
    latest = breadth.dropna().iloc[-1]
    assert latest["20MA_Ratio"] == 100.0
    assert latest["50MA_Ratio"] == 100.0
    assert latest["200MA_Ratio"] == 100.0
