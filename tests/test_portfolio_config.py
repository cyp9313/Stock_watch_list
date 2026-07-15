from multiuser_store import config_to_api_groups, normalize_config


def test_normalize_config_adds_empty_portfolio_pages_for_legacy_configs():
    config = normalize_config({
        "stocks_pages": [{"name": "Stocks", "groups": {"Core": ["AAPL"]}}],
        "broad_pages": [{"name": "Macro", "groups": {"Index": ["^GSPC"]}}],
    })

    assert config["portfolio_pages"] == [{"name": "Portfolio", "holdings": []}]


def test_portfolio_tickers_are_included_in_stock_data_api_groups():
    config = normalize_config({
        "stocks_pages": [{"name": "Stocks", "groups": {"Core": ["AAPL"]}}],
        "broad_pages": [{"name": "Macro", "groups": {"Index": ["^GSPC"]}}],
        "portfolio_pages": [{
            "name": "My Portfolio",
            "holdings": [
                {
                    "group": "Longs",
                    "ticker": "TSM",
                    "buy_price": 100,
                    "shares": 2,
                    "buy_currency": "USD",
                }
            ],
        }],
    })

    groups = config_to_api_groups(config)

    assert groups["P:My Portfolio:Longs"] == ["TSM"]
