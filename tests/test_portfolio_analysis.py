from __future__ import annotations

import pandas as pd

from portfolio_analysis import (
    build_portfolio_snapshot,
    calculate_portfolio_metrics,
    rank_portfolio_risks,
    validate_portfolio_advice,
)


def _page():
    return {
        "id": "pf_test",
        "name": "Test Portfolio",
        "analysis_settings": {"base_currency": "EUR", "benchmark": "^GSPC"},
        "holdings": [
            {"group": "AI", "ticker": "AAA", "buy_price": 50, "shares": 10, "buy_currency": "USD"},
            {"group": "AI", "ticker": "BBB.DE", "buy_price": 80, "shares": 5, "buy_currency": "EUR"},
        ],
    }


def test_snapshot_converts_to_base_currency_and_weights():
    snapshot = build_portfolio_snapshot(
        _page(),
        [{"Ticker": "AAA", "Price": 100, "Currency": "USD"}, {"Ticker": "BBB.DE", "Price": 100, "Currency": "EUR"}],
        fx_rates={"USDEUR": 0.9},
        base_currency="EUR",
    )

    assert snapshot["summary"]["total_market_value_base"] == 1400
    weights = {h["ticker"]: h["weight"] for h in snapshot["holdings"]}
    assert round(weights["AAA"], 4) == round(900 / 1400, 4)
    assert round(weights["BBB.DE"], 4) == round(500 / 1400, 4)


def test_snapshot_prefers_ui_market_row_price_over_downloaded_price():
    snapshot = build_portfolio_snapshot(
        _page(),
        [{"Ticker": "AAA", "Price": 100, "Currency": "USD"}],
        latest_prices={"AAA": 999},
        fx_rates={"USDEUR": 0.9},
        base_currency="EUR",
    )

    first = snapshot["holdings"][0]
    assert first["price"] == 100
    assert first["market_value_base"] == 900


def test_metrics_and_risk_ranking_are_deterministic():
    snapshot = build_portfolio_snapshot(
        _page(),
        [{"Ticker": "AAA", "Price": 100, "Currency": "USD"}, {"Ticker": "BBB.DE", "Price": 100, "Currency": "EUR"}],
        fx_rates={"USDEUR": 0.9},
        base_currency="EUR",
    )
    close = pd.DataFrame({
        "AAA": [90, 95, 100, 105, 110],
        "BBB.DE": [100, 98, 96, 94, 92],
        "^GSPC": [100, 101, 102, 103, 104],
    })
    metrics = calculate_portfolio_metrics(snapshot, close, benchmark="^GSPC")
    ranking = rank_portfolio_risks(snapshot, metrics)

    assert metrics["top1_weight"] > 0.6
    assert metrics["hhi"] > 0
    assert ranking["top_risk_tickers"]


def test_advice_validation_drops_bad_tickers_and_exact_share_fields():
    snapshot = build_portfolio_snapshot(
        _page(),
        [{"Ticker": "AAA", "Price": 100, "Currency": "USD"}],
        fx_rates={"USDEUR": 0.9},
        base_currency="EUR",
    )
    advice = validate_portfolio_advice(
        {
            "confidence": 2,
            "actions": [
                {"ticker": "AAA", "action": "sell_now", "shares_to_sell": 3, "target_weight_min": -1, "target_weight_max": 2},
                {"ticker": "ZZZ", "action": "exit"},
            ],
        },
        snapshot,
        [],
    )

    assert len(advice["actions"]) == 1
    assert advice["actions"][0]["action"] == "watch"
    assert "shares_to_sell" not in advice["actions"][0]
    assert advice["actions"][0]["current_weight"] == snapshot["holdings"][0]["weight"]
    assert advice["confidence"] == 1.0
