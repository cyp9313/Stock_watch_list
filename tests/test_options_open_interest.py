import datetime as dt

import pandas as pd
import pytest

from options_open_interest import (
    aggregate_open_interest,
    calculate_dealer_gex,
    option_gamma_legs,
    select_option_expirations,
)


def test_select_option_expirations_uses_nearest_future_and_three_month_window():
    nearest, horizon, through = select_option_expirations(
        ["2026-07-24", "2026-07-31", "2026-08-21", "2026-10-23", "2026-11-20"],
        dt.date(2026, 7, 25),
    )
    assert nearest == "2026-07-31"
    assert horizon == ["2026-07-31", "2026-08-21", "2026-10-23"]
    assert through == "2026-10-25"


def test_select_option_expirations_supports_one_to_twelve_month_horizons_only():
    _, horizon, through = select_option_expirations(
        ["2026-07-31", "2026-08-21", "2027-07-16"], dt.date(2026, 7, 25), months=1,
    )
    assert horizon == ["2026-07-31", "2026-08-21"]
    assert through == "2026-08-25"
    with pytest.raises(ValueError, match="1 and 12"):
        select_option_expirations([], dt.date(2026, 7, 25), months=13)


def test_aggregate_open_interest_sums_calls_and_puts_by_strike_and_ignores_invalid_rows():
    first = {
        "calls": pd.DataFrame({"strike": [100, 105, None], "openInterest": [10, 30, 99]}),
        "puts": pd.DataFrame({"strike": [100, 110], "openInterest": [20, 40]}),
    }
    second = {
        "calls": pd.DataFrame({"strike": [100, 110], "openInterest": [5, 7]}),
        "puts": pd.DataFrame({"strike": [105, 110], "openInterest": [6, -1]}),
    }
    assert aggregate_open_interest([first, second]) == [
        {"strike": 100.0, "calls": 15, "puts": 20},
        {"strike": 105.0, "calls": 30, "puts": 6},
        {"strike": 110.0, "calls": 7, "puts": 40},
    ]


def test_option_gamma_legs_retains_only_valid_oi_iv_inputs():
    chain = {
        "calls": pd.DataFrame({
            "strike": [100, 105], "openInterest": [10, 0], "impliedVolatility": [0.25, 0.30],
        }),
        "puts": pd.DataFrame({
            "strike": [100, 110], "openInterest": [5, 20], "impliedVolatility": [0.25, None],
        }),
    }
    assert option_gamma_legs(chain, "2026-08-21") == [
        {
            "expiration": "2026-08-21", "side": "call", "strike": 100.0,
            "open_interest": 10, "implied_volatility": 0.25, "multiplier": 100,
        },
        {
            "expiration": "2026-08-21", "side": "put", "strike": 100.0,
            "open_interest": 5, "implied_volatility": 0.25, "multiplier": 100,
        },
    ]


def test_dealer_gex_uses_positive_calls_negative_puts_and_reprices_with_spot():
    legs = [
        {"expiration": "2026-08-21", "side": "call", "strike": 100, "open_interest": 10, "implied_volatility": 0.25, "multiplier": 100},
        {"expiration": "2026-08-21", "side": "put", "strike": 100, "open_interest": 5, "implied_volatility": 0.25, "multiplier": 100},
    ]
    now = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    at_100 = calculate_dealer_gex(legs, 100, risk_free_rate=0.04, dividend_yield=0.01, now=now)
    at_110 = calculate_dealer_gex(legs, 110, risk_free_rate=0.04, dividend_yield=0.01, now=now)
    assert len(at_100) == 1
    assert at_100[0]["call_gex"] > 0
    assert at_100[0]["put_gex"] < 0
    assert at_100[0]["dealer_gex"] == pytest.approx(at_100[0]["call_gex"] + at_100[0]["put_gex"])
    assert at_100[0]["dealer_gex"] > 0
    assert at_110[0]["dealer_gex"] != pytest.approx(at_100[0]["dealer_gex"])


def test_dealer_gex_ignores_invalid_contracts_and_invalid_spot():
    legs = [
        {"expiration": "2026-08-21", "side": "call", "strike": 100, "open_interest": 10, "implied_volatility": 0.25},
        {"expiration": "bad", "side": "put", "strike": 100, "open_interest": 10, "implied_volatility": 0.25},
    ]
    now = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    assert len(calculate_dealer_gex(legs, 100, now=now)) == 1
    assert calculate_dealer_gex(legs, None, now=now) == []
